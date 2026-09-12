"""The proxy pipeline: identify client, forward to upstream, tap the response, capture.

Transparent tap-and-forward: the request goes to the upstream verbatim and the
upstream response (SSE streams included) is streamed back to the client
untouched, while a side parse (the dissector) accumulates the exchange for the
store. The tap never blocks the client path. The selected dissector decodes
what it recognizes; anything else is captured opaquely (raw, capped) and
logged at INFO.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

import httpx2
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .. import dissectors  # noqa: F401  (registers the bundled dissectors)
from ..dissectors.base import get_dissector
from ..hub import Hub
from ..model.conversation import Conversation, Exchange
from ..model.ir import ParsedRequest, WireRequest
from ..store.memory import MemoryStore
from .util import safe_json

# Hop-by-hop / transport headers we must not forward upstream (httpx2 rebuilds them).
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
# Response headers we strip before sending back to the client (httpx2 already
# decompressed, and Starlette recomputes content-length).
_STRIP_RESP = {"content-length", "content-encoding", "transfer-encoding", "connection", "date", "server"}
# Headers masked in captured exchanges/dumps.
_SECRET_HEADERS = {"authorization", "proxy-authorization", "x-api-key"}

# Raw (non-JSON) request/response bodies and undecoded SSE streams are stored
# capped, so the ring-bounded store stays bounded for large payloads.
_RAW_BODY_CAP = 64 * 1024

# Client ids are embedded in REST paths (/api/conversations/{cid}), so they are
# restricted to a single-segment character set (no "/" or ":").
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

log = logging.getLogger("llm_proxy.proxy")


def _capped_text(data: bytes) -> str | None:
    """Best-effort UTF-8 text for a raw (non-JSON) body, capped for storage."""
    if not data:
        return None
    return data[:_RAW_BODY_CAP].decode("utf-8", errors="replace")


def _looks_binary(data: bytes) -> bool:
    """Heuristic for a binary (non-text) body: a NUL byte, or bytes that are not
    valid UTF-8. Binary bodies (images, other non-text assets from a proxied
    non-LLM endpoint) are recorded by content-type and size, not decoded to
    text. Only a capped prefix is inspected - binary magic bytes lead, and a
    multibyte character split at the cap is tolerated (drop up to three trailing
    bytes before deciding)."""
    if not data:
        return False
    sample = data[:_RAW_BODY_CAP]
    if b"\x00" in sample:
        return True
    for trim in range(min(4, len(sample))):
        try:
            sample[: len(sample) - trim].decode("utf-8")
            return False
        except UnicodeDecodeError:
            continue
    return True


def _content_type(headers: dict) -> str | None:
    """The body's content-type (without parameters, lowercased) from a wire
    header dict, if the request/response declared one."""
    for k, v in headers.items():
        if k.lower() == "content-type":
            ct = v.split(";", 1)[0].strip().lower()
            return ct or None
    return None


def _is_opaque(headers: dict, body: bytes) -> bool:
    """Whether a wire body is captured opaquely (content-type + size, no decode).

    A declared content-type decides: JSON (``application/json`` or ``*/+json``)
    and ``text/plain`` are decoded; any other type - CSS, JS, HTML, SVG, fonts,
    images, octet-stream, ... - is opaque. With no declared type, a binary body
    is opaque and a text one is decoded best-effort. The proxy taps LLM APIs,
    not a browser's asset tree, so opaque keeps the store and the UI readable.
    """
    ct = _content_type(headers)
    if ct is None:
        return _looks_binary(body)
    return ct not in ("application/json", "text/plain") and not ct.endswith("+json")


def _mask(v) -> str:
    if not v:
        return v
    s = str(v)
    return s if len(s) <= 8 else f"{s[:4]}***{s[-2:]}"


def _redact(headers: dict) -> dict:
    return {k: (_mask(v) if k.lower() in _SECRET_HEADERS else v) for k, v in headers.items()}


class Pipeline:
    def __init__(self, http: httpx2.AsyncClient, settings, store: MemoryStore, hub: Hub):
        self.http = http
        self.settings = settings
        self.store = store
        self.hub = hub
        # Selection is per request by method+path: the chat decoder when the path
        # is an OpenAI-compatible chat completion, else the generic raw-capture
        # fallback. No global setting; the pipeline branches on ``is self.generic``
        # for the opaque INFO log.
        self.chat = get_dissector("openai")
        self.generic = get_dissector("generic")

    def dissector_for(self, method: str, path: str):
        """The dissector for this request: the chat decoder if the path is an
        OpenAI-compatible chat completion, else the generic raw-capture fallback."""
        return self.chat if self.chat.matches(method, path) else self.generic

    def _parse_request(self, dissector, wire_req) -> ParsedRequest:
        try:
            return dissector.request(wire_req)
        except Exception:  # noqa: BLE001
            # A tap must never break the forward path: fall back to raw capture.
            log.warning(
                "dissector %s failed to parse the request; capturing raw", dissector.name, exc_info=True
            )
            return self.generic.request(wire_req)

    def _parse_response(self, dissector, wire_req, status: int, resp_headers: dict, data: bytes):
        """Run the dissector's response lifecycle over a fully drained body."""
        try:
            handle = dissector.response_started(wire_req, status, resp_headers)
            if handle is None:
                return None
            dissector.feed_chunk(handle, data)
            return dissector.finalize(handle)
        except Exception:  # noqa: BLE001
            # A tap must never break the forward path: fall back to raw capture.
            log.warning(
                "dissector %s failed to decode the response; capturing raw", dissector.name, exc_info=True
            )
            return None

    # ---- client identity ----
    @staticmethod
    def _extract_key(headers: dict) -> str | None:
        auth = headers.get("authorization") or headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
        return None

    @staticmethod
    def _id_part(value: str) -> str | None:
        return _ID_UNSAFE.sub("_", value.strip())[:64] or None

    def client_id(self, request: Request) -> str:
        """Source IP, + user agent, + API key. The user agent splits clients that
        share a source IP (e.g. curl and an editor client on the same machine).
        """
        headers = dict(request.headers)
        if self.settings.client_id_header:
            override = self._id_part(headers.get(self.settings.client_id_header.lower(), ""))
            if override:
                return override
        parts = [self._id_part(request.client.host if request.client else "unknown")]
        ua = self._id_part(headers.get("user-agent", ""))
        if ua:
            parts.append(ua)
        key = self._id_part(self._extract_key(headers) or "")
        if key:
            parts.append(key)
        return "::".join(parts)

    # ---- upstream request shaping (transparent passthrough + key fallback) ----
    def _upstream_headers(self, headers: dict) -> dict:
        out = {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}
        if not any(k.lower() == "authorization" for k in out) and self.settings.upstream_api_key:
            out["authorization"] = f"Bearer {self.settings.upstream_api_key}"
        return out

    def _replay_headers(self, headers: dict) -> dict:
        # Captured headers are redacted, so any secret values are unusable masks;
        # drop them and let _upstream_headers() re-apply the UPSTREAM_API_KEY fallback.
        out = {k: v for k, v in headers.items() if k.lower() not in _SECRET_HEADERS}
        return self._upstream_headers(out)

    @staticmethod
    def _resp_headers(headers: dict) -> dict:
        return {k: v for k, v in headers.items() if k.lower() not in _STRIP_RESP}

    # ---- entry point (called by the router for every request on the LLM port) ----
    async def handle(self, request: Request) -> Response:
        t0 = time.time()
        body = await request.body()
        # Forward the full path *including the query string* verbatim: a
        # transparent proxy must not drop ``?model=...`` and friends.
        url = request.url
        full_path = f"{url.path}?{url.query}" if url.query else url.path
        wire_req = WireRequest(
            method=request.method,
            path=full_path,
            headers=dict(request.headers),
            body=body,
            body_json=safe_json(body),
        )

        tag = request.headers.get("x-conversation-id", "") if self.settings.split_conversations else ""
        client_id = self.client_id(request)
        headers = self._upstream_headers(dict(request.headers))

        dissector = self.dissector_for(wire_req.method, wire_req.path)
        if dissector is self.generic:
            log.debug("undecoded request: %s %s (opaque capture)", wire_req.method, wire_req.path)
        parsed_req = self._parse_request(dissector, wire_req)

        cid = self.store.conversation_id(client_id, tag)
        exchange_id = str(uuid.uuid4())

        if not self.store.has_client(client_id):
            # Register the client BEFORE broadcasting: client_seen triggers an
            # immediate /api/clients re-fetch in the UI, which must never run
            # ahead of the store or it returns an empty dock. handle() runs
            # synchronously up to its first await, so anything registered here
            # is visible before the re-fetch can be processed (no race window).
            self.store.get_or_create_conversation(client_id, tag)
            self.hub.emit(
                cid,
                {"type": "client_seen", "conversation_id": cid, "client_id": client_id, "ts": t0},
                broadcast=True,
            )
        return await self._forward(
            wire_req, parsed_req, dissector, client_id, tag, cid, exchange_id, headers, t0, is_replay=False
        )

    # ---- forward to upstream (shared by live requests and replay) ----
    async def _forward(
        self,
        wire_req,
        parsed_req,
        dissector,
        client_id,
        tag,
        cid,
        exchange_id,
        headers,
        t0,
        *,
        is_replay: bool,
    ) -> Response:
        # Display hint for the pending card; the real stream decision is made
        # from the upstream response's content-type in _upstream().
        is_stream = bool(wire_req.body_json and wire_req.body_json.get("stream"))
        # An opaque request body (e.g. an uploaded image or form payload) is not
        # decoded to text: record its content-type and size instead.
        req_opaque = _is_opaque(wire_req.headers, wire_req.body)
        # Full captured request shape (headers/body redacted), plus model/preview
        # for the card header. Shared by the in-flight placeholder and the
        # exchange_started event so the pending card's debug views are complete.
        client_request = {
            "timestamp": t0,
            "method": wire_req.method,
            "path": wire_req.path,
            "headers": _redact(wire_req.headers),
            "content_type": _content_type(wire_req.headers),
            "body_json": wire_req.body_json,
            "opaque": req_opaque,
            "body_text": (
                None if (wire_req.body_json is not None or req_opaque) else _capped_text(wire_req.body)
            ),
            "size_bytes": len(wire_req.body) if wire_req.body else 0,
            "model": parsed_req.model,
            "preview": parsed_req.preview,
        }
        # Register in-flight before responding: the exchange_started event is
        # focus-scoped and a UI that only now focuses this conversation misses
        # it, so REST must already show the pending exchange. The sequence is
        # assigned here (conv.append) and forwarded in the event so the pending
        # card - and any replay from it - targets the right exchange; a UI-side
        # guess desyncs behind a slow in-flight request.
        pending = Exchange(
            id=exchange_id,
            is_replay=is_replay,
            client_request=client_request,
            in_flight=True,
            streaming=is_stream,
            dissector=dissector.name,
        )
        self.store.begin_exchange(client_id, tag, pending)
        self.hub.emit(
            cid,
            {
                "type": "exchange_started",
                "conversation_id": cid,
                "exchange_id": exchange_id,
                "sequence": pending.sequence,
                "streaming": is_stream,
                "is_replay": is_replay,
                "dissector": dissector.name,
                "ts": t0,
                "client_request": client_request,
            },
            dock=True,
        )

        try:
            return await self._upstream(
                wire_req,
                dissector,
                client_request,
                client_id,
                tag,
                cid,
                exchange_id,
                headers,
                t0,
                is_replay=is_replay,
            )
        except asyncio.CancelledError:
            # The client went away before the upstream responded (e.g. gave up on
            # a slow request). Capture and emit so the in-flight placeholder
            # resolves instead of lingering as a stuck "pending" card.
            t_end = time.time()
            ex = self._capture(
                client_id,
                tag,
                wire_req,
                client_request,
                exchange_id=exchange_id,
                status=499,
                resp_headers={},
                streaming=False,
                body=b"",
                dissector=dissector.name,
                error={"type": "CancelledError", "message": "client disconnected before upstream responded"},
                t0=t0,
                t_send=t0,
                t_first=None,
                t_end=t_end,
                is_replay=is_replay,
            )
            if ex is not None:
                self.hub.emit(
                    cid,
                    {
                        "type": "exchange_completed",
                        "conversation_id": cid,
                        "exchange_id": exchange_id,
                        "ts": t_end,
                        "exchange": ex.to_dict(),
                    },
                    dock=True,
                )
            raise

    # ---- replay (re-send a captured client request to the upstream) ----
    async def replay(self, conv: Conversation, source: Exchange, body: dict | None) -> Response:
        cr = source.client_request or {}
        # The UI sends the full edited request body; an empty/missing body means
        # replay as captured (e.g. GETs, or an unedited dock).
        body_json = dict(body) if isinstance(body, dict) and body else dict(cr.get("body_json") or {})
        body_bytes = json.dumps(body_json).encode()
        wire_req = WireRequest(
            method=cr.get("method", "POST"),
            path=cr.get("path", "/v1/chat/completions"),
            headers=dict(cr.get("headers") or {}),
            body=body_bytes,
            body_json=body_json,
        )
        client_id = conv.client_id
        tag = conv.tag
        cid = conv.id
        headers = self._replay_headers(cr.get("headers") or {})
        dissector = self.dissector_for(wire_req.method, wire_req.path)
        parsed_req = self._parse_request(dissector, wire_req)
        t0 = time.time()
        exchange_id = str(uuid.uuid4())
        return await self._forward(
            wire_req, parsed_req, dissector, client_id, tag, cid, exchange_id, headers, t0, is_replay=True
        )

    # ---- upstream call: open the stream, branch on the response's content-type ----
    async def _upstream(
        self,
        wire_req,
        dissector,
        client_request,
        client_id,
        tag,
        cid,
        exchange_id,
        headers,
        t0,
        *,
        is_replay: bool,
    ) -> Response:
        # Open the upstream stream *before* responding, so the client receives the
        # upstream's real status/headers (a clean 502 if it is unreachable) rather
        # than a premature 200 that is then aborted mid-body.
        t_send = time.time()
        # Hold the stream context manager alive across the response: httpx2's
        # stream() is an @asynccontextmanager, and dropping it would let it be
        # GC'd, closing the response stream before the body is read.
        stream_cm = self.http.stream(
            wire_req.method, wire_req.path, content=wire_req.body or None, headers=headers
        )
        try:
            up = await stream_cm.__aenter__()
            status = up.status_code
            resp_headers = dict(up.headers)
            if "text/event-stream" in resp_headers.get("content-type", ""):
                return await self._sse_stream(
                    up,
                    stream_cm,
                    wire_req,
                    dissector,
                    client_request,
                    client_id,
                    tag,
                    cid,
                    exchange_id,
                    status,
                    resp_headers,
                    t0,
                    t_send,
                    is_replay=is_replay,
                )

            # Non-SSE response (JSON, text, or an upstream error): drain it fully,
            # tap it, and return it verbatim.
            state: dict = {"first": None, "end": None}
            data = b""
            try:
                async for chunk in up.aiter_bytes():
                    if state["first"] is None:
                        state["first"] = time.time()
                    data += chunk
            finally:
                state["end"] = time.time()
                await stream_cm.__aexit__(None, None, None)
        except Exception as exc:  # noqa: BLE001 - upstream failed before the
            # client received anything (request, response, or a mid-drain death):
            # a clean 502, captured as a failed exchange.
            t_end = time.time()
            ex = self._capture(
                client_id,
                tag,
                wire_req,
                client_request,
                exchange_id=exchange_id,
                status=502,
                resp_headers={},
                streaming=False,
                body=b"",
                dissector=dissector.name,
                error={"type": type(exc).__name__, "message": str(exc)},
                t0=t0,
                t_send=t_send,
                t_first=None,
                t_end=t_end,
                is_replay=is_replay,
            )
            if ex is not None:
                self.hub.emit(
                    cid,
                    {
                        "type": "exchange_completed",
                        "conversation_id": cid,
                        "exchange_id": exchange_id,
                        "ts": t_end,
                        "exchange": ex.to_dict(),
                    },
                    dock=True,
                )
            return Response(
                content=json.dumps({"error": {"message": f"upstream error: {exc}"}}).encode(),
                status_code=502,
                media_type="application/json",
            )
        parsed = self._parse_response(dissector, wire_req, status, resp_headers, data)
        ex = self._capture(
            client_id,
            tag,
            wire_req,
            client_request,
            exchange_id=exchange_id,
            status=status,
            resp_headers=resp_headers,
            streaming=False,
            body=data,
            parsed=parsed,
            dissector=dissector.name,
            t0=t0,
            t_send=t_send,
            t_first=state["first"],
            t_end=state["end"],
            is_replay=is_replay,
        )
        if ex is not None:
            self.hub.emit(
                cid,
                {
                    "type": "exchange_completed",
                    "conversation_id": cid,
                    "exchange_id": exchange_id,
                    "ts": state["end"],
                    "exchange": ex.to_dict(),
                },
                dock=True,
            )
        return Response(
            content=data,
            status_code=status,
            headers=self._resp_headers(resp_headers),
            media_type=resp_headers.get("content-type"),
        )

    # ---- SSE stream: tap-and-forward with live UI fan-out (decoded chat) or a
    # capped raw capture (undecoded) ----
    async def _sse_stream(
        self,
        up,
        stream_cm,
        wire_req,
        dissector,
        client_request,
        client_id,
        tag,
        cid,
        exchange_id,
        status,
        resp_headers,
        t0,
        t_send,
        *,
        is_replay: bool,
    ) -> StreamingResponse:
        state: dict = {"first": None, "end": None, "first_content": None, "last_content": None, "size": 0}
        try:
            handle = dissector.response_started(wire_req, status, resp_headers)
        except Exception:  # noqa: BLE001
            log.warning(
                "dissector %s failed to open the response; capturing raw", dissector.name, exc_info=True
            )
            handle = None
        raw_chunks: list[bytes] = []
        raw_parts: list[bytes] = []
        raw_len = 0

        async def generate():
            nonlocal raw_len
            try:
                async for chunk in up.aiter_bytes():
                    if state["first"] is None:
                        state["first"] = time.time()
                    state["size"] += len(chunk)
                    if handle is not None:
                        try:
                            deltas = dissector.feed_chunk(handle, chunk)
                        except Exception:  # noqa: BLE001
                            deltas = []
                        for d in deltas:
                            if d.delta or d.reasoning_delta:
                                now = time.time()
                                if d.delta:
                                    if state["first_content"] is None:
                                        state["first_content"] = now
                                    state["last_content"] = now
                                self.hub.emit(
                                    cid,
                                    {
                                        "type": "delta",
                                        "conversation_id": cid,
                                        "exchange_id": exchange_id,
                                        "delta": d.delta,
                                        "reasoning_delta": d.reasoning_delta,
                                        "ts": now,
                                    },
                                )
                    if self.settings.include_raw_chunks:
                        raw_chunks.append(chunk)
                    if raw_len < _RAW_BODY_CAP:
                        # Keep a capped raw copy of every stream: the raw SSE is
                        # the faithful capture when the dissector does not
                        # reassemble it (undecoded, or a non-chat recognized path).
                        room = _RAW_BODY_CAP - raw_len
                        raw_parts.append(chunk[:room])
                        raw_len += min(len(chunk), room)
                    yield chunk
            except httpx2.TransportError as exc:
                # Upstream died mid-stream: end it cleanly (the finally below
                # still records the partial capture) instead of crashing ASGI.
                log.warning("upstream stream interrupted: %s %s: %s", wire_req.method, wire_req.path, exc)
            finally:
                state["end"] = time.time()
                await stream_cm.__aexit__(None, None, None)
                parsed = None
                if handle is not None:
                    try:
                        parsed = dissector.finalize(handle)
                    except Exception:  # noqa: BLE001
                        parsed = None
                ex = self._capture(
                    client_id,
                    tag,
                    wire_req,
                    client_request,
                    exchange_id=exchange_id,
                    status=status,
                    resp_headers=resp_headers,
                    streaming=True,
                    raw_text="".join(p.decode("utf-8", errors="replace") for p in raw_parts),
                    raw_chunks=raw_chunks,
                    body_size=state["size"],
                    parsed=parsed,
                    dissector=dissector.name,
                    t0=t0,
                    t_send=t_send,
                    t_first=state["first"],
                    t_end=state["end"],
                    t_first_content=state["first_content"],
                    t_last_content=state["last_content"],
                    is_replay=is_replay,
                )
                if ex is not None:
                    self.hub.emit(
                        cid,
                        {
                            "type": "exchange_completed",
                            "conversation_id": cid,
                            "exchange_id": exchange_id,
                            "ts": state["end"],
                            "exchange": ex.to_dict(),
                        },
                        dock=True,
                    )

        return StreamingResponse(
            generate(),
            status_code=status,
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    # ---- capture (build an Exchange and store it) ----
    def _capture(
        self,
        client_id,
        tag,
        wire_req,
        client_request,
        *,
        exchange_id,
        status,
        resp_headers,
        streaming,
        body=None,
        body_size=None,
        raw_text=None,
        raw_chunks=None,
        parsed=None,
        dissector="generic",
        error=None,
        t0,
        t_send,
        t_first,
        t_end,
        t_first_content=None,
        t_last_content=None,
        is_replay: bool = False,
    ) -> Exchange | None:
        body_bytes = body if body is not None else wire_req.body
        # Upstream-reported generation rate: llama.cpp embeds a ``timings`` object
        # (``predicted_n``/``predicted_ms``) in the final stream chunk and in
        # non-stream bodies. It is the only reliable tok/s for non-stream responses
        # and for reasoning models, where byte-level windows mislead.
        up_timings = parsed.timings if parsed is not None else None
        if up_timings is None and body_bytes:
            up_timings = (safe_json(body_bytes) or {}).get("timings")
        gen_tok_per_sec = None
        if isinstance(up_timings, dict):
            pn = up_timings.get("predicted_n")
            pms = up_timings.get("predicted_ms")
            if isinstance(pn, (int, float)) and isinstance(pms, (int, float)) and pms > 0:
                gen_tok_per_sec = round(pn / (pms / 1000), 1)
        timings = {
            "t_request_in": t0,
            "t_upstream_send": t_send,
            "t_first_byte": t_first,
            "t_first_content": t_first_content,
            "t_last_content": t_last_content,
            "t_end": t_end,
            "ttft_ms": round((t_first - t_send) * 1000, 2) if (t_first and t_send) else None,
            "total_ms": round((t_end - t0) * 1000, 2) if (t_end and t0) else None,
            "gen_tok_per_sec": gen_tok_per_sec,
        }
        usage = None
        u = parsed.usage if parsed is not None else None
        if not isinstance(u, dict) and body_bytes:
            u = (safe_json(body_bytes) or {}).get("usage")
        if isinstance(u, dict):
            usage = {
                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                "completion_tokens": int(u.get("completion_tokens") or 0),
                "total_tokens": int(u.get("total_tokens") or 0),
            }

        # A proxied non-LLM endpoint serves the web UI's assets (CSS, JS, HTML,
        # SVG, images, fonts, ...); they are captured opaquely - content-type +
        # size, no decoded body. (For the non-stream case ``body`` is the response
        # body; streams are always SSE text, so this is gated on ``not streaming``.)
        resp_opaque = not streaming and _is_opaque(resp_headers, body or b"")
        server_response: dict = {
            "timestamp": t_first,
            "status": status,
            "headers": _redact(resp_headers),
            "streaming": streaming,
            "content_type": _content_type(resp_headers),
            "opaque": resp_opaque,
            "size_bytes": body_size if body_size is not None else (len(body_bytes) if body_bytes else 0),
        }
        if streaming and parsed is not None and parsed.reassembled is not None:
            server_response["reassembled"] = parsed.reassembled
        elif raw_text is not None:
            # Undecoded stream: the capped raw stream is the only faithful capture.
            server_response["body_text"] = raw_text
        elif resp_opaque:
            # Opaque capture: content_type + size_bytes above are the record; the
            # body is forwarded verbatim and not stored.
            pass
        else:
            body_json = parsed.body_json if parsed is not None else None
            if body_json is None:
                body_json = safe_json(body_bytes)
            server_response["body_json"] = body_json
            if body_json is None:
                server_response["body_text"] = _capped_text(body_bytes)
        if self.settings.include_raw_chunks and raw_chunks is not None:
            server_response["chunks"] = [c.decode("utf-8", errors="replace") for c in raw_chunks]

        exchange = Exchange(
            id=exchange_id,
            is_replay=is_replay,
            client_request=client_request,
            server_response=server_response,
            timings=timings,
            usage=usage,
            error=error,
            dissector=dissector,
        )
        # add_exchange finalizes the in-flight placeholder in place and returns the
        # copy that lives in the ring buffer, whose sequence was assigned at
        # begin_exchange. Return it (not the freshly-built one) so the completed
        # event carries the real sequence rather than the default 0. It is None if
        # the exchange was removed while in flight: callers then skip the completed
        # event, since there is no stored exchange to point at.
        _, _, stored = self.store.add_exchange(client_id, tag, exchange)
        return stored
