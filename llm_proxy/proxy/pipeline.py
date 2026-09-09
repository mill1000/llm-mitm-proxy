"""The proxy pipeline: identify client, forward to upstream, tap the response, capture.

Core behavior (v1 same-format OpenAI -> OpenAI) is **tap-and-forward**: the upstream
response (including SSE streams) is streamed back to the client untouched, while a
side parse accumulates the exchange for the store. The tap never blocks the client path.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

import httpx2
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from ..adapters.base import AdapterRegistry
from ..hub import Hub
from ..model.conversation import Conversation, Exchange
from ..model.ir import WireRequest, WireResponse
from ..store.memory import MemoryStore
from .sse import SSEStream

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


# Client ids are embedded in REST paths (/api/conversations/{cid}), so they are
# restricted to a single-segment character set (no "/" or ":").
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_json(data) -> dict | None:
    if not data:
        return None
    try:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        obj = json.loads(data)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def _mask(v) -> str:
    if not v:
        return v
    s = str(v)
    return s if len(s) <= 8 else f"{s[:4]}***{s[-2:]}"


def _redact(headers: dict) -> dict:
    return {k: (_mask(v) if k.lower() in _SECRET_HEADERS else v) for k, v in headers.items()}


def _delta_parts(data: dict | None) -> tuple[str, str]:
    """Concatenate one SSE chunk's ``choices[].delta`` content and reasoning text."""
    if not data:
        return "", ""
    content = ""
    reasoning = ""
    for c in data.get("choices", []) or []:
        delta = c.get("delta") or {}
        piece = delta.get("content")
        if piece:
            content += piece
        rp = delta.get("reasoning_content")
        if rp:
            reasoning += rp
    return content, reasoning


class Pipeline:
    def __init__(
        self, http: httpx2.AsyncClient, settings, registry: AdapterRegistry, store: MemoryStore, hub: Hub
    ):
        self.http = http
        self.settings = settings
        self.store = store
        self.hub = hub
        self.in_adapter = registry.in_adapter(settings.in_adapter)
        self.out_adapter = registry.out_adapter(settings.out_adapter)
        self.same_format = settings.in_adapter == settings.out_adapter

    @staticmethod
    def _last_user_preview(body_json: dict | None, limit: int = 160) -> str | None:
        """A short, UI-friendly preview of the last user message (no full body over WS)."""
        if not body_json:
            return None
        for m in reversed(body_json.get("messages", []) or []):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c if len(c) <= limit else c[:limit] + "…"
                if c:
                    return str(c)[:limit]
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

    # ---- entry point (called by the router for every /v1/* request) ----
    async def handle(self, request: Request) -> Response:
        t0 = time.time()
        body = await request.body()
        wire_req = WireRequest(
            method=request.method,
            path=request.url.path,
            headers=dict(request.headers),
            body=body,
            body_json=_safe_json(body),
        )

        tag = request.headers.get("x-conversation-id", "") if self.settings.split_conversations else ""
        client_id = self.client_id(request)
        headers = self._upstream_headers(dict(request.headers))

        try:
            norm_req = self.in_adapter.parse_request(wire_req)
        except Exception:
            norm_req = None

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
            wire_req, norm_req, client_id, tag, cid, exchange_id, headers, t0, is_replay=False
        )

    # ---- forward to upstream (shared by live requests and replay) ----
    async def _forward(
        self, wire_req, norm_req, client_id, tag, cid, exchange_id, headers, t0, *, is_replay: bool
    ) -> Response:
        is_stream = bool(wire_req.body_json and wire_req.body_json.get("stream"))
        # Full captured request shape (headers/body redacted), plus model/preview
        # for the card header. Shared by the in-flight placeholder and the
        # exchange_started event so the pending card's debug views are complete.
        client_request = {
            "timestamp": t0,
            "method": wire_req.method,
            "path": wire_req.path,
            "headers": _redact(wire_req.headers),
            "body_json": wire_req.body_json,
            "size_bytes": len(wire_req.body) if wire_req.body else 0,
            "model": (wire_req.body_json or {}).get("model"),
            "preview": self._last_user_preview(wire_req.body_json),
        }
        # Register in-flight before responding: the exchange_started event is
        # focus-scoped and a UI that only now focuses this conversation misses
        # it, so REST must already show the pending exchange.
        self.store.begin_exchange(
            client_id,
            tag,
            Exchange(
                id=exchange_id,
                is_replay=is_replay,
                client_request=client_request,
                in_flight=True,
                streaming=is_stream,
            ),
        )
        self.hub.emit(
            cid,
            {
                "type": "exchange_started",
                "conversation_id": cid,
                "exchange_id": exchange_id,
                "streaming": is_stream,
                "is_replay": is_replay,
                "ts": t0,
                "client_request": client_request,
            },
            dock=True,
        )

        try:
            if is_stream:
                return await self._stream(
                    wire_req, norm_req, client_id, tag, cid, exchange_id, headers, t0, is_replay=is_replay
                )
            return await self._non_stream(
                wire_req, norm_req, client_id, tag, cid, exchange_id, headers, t0, is_replay=is_replay
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
                norm_req,
                exchange_id=exchange_id,
                status=499,
                resp_headers={},
                streaming=False,
                body=b"",
                error={"type": "CancelledError", "message": "client disconnected before upstream responded"},
                t0=t0,
                t_send=t0,
                t_first=None,
                t_end=t_end,
                is_replay=is_replay,
            )
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
        try:
            norm_req = self.in_adapter.parse_request(wire_req)
        except Exception:  # noqa: BLE001
            norm_req = None
        t0 = time.time()
        exchange_id = str(uuid.uuid4())
        return await self._forward(
            wire_req, norm_req, client_id, tag, cid, exchange_id, headers, t0, is_replay=True
        )

    # ---- non-streaming ----
    async def _non_stream(
        self, wire_req, norm_req, client_id, tag, cid, exchange_id, headers, t0, *, is_replay: bool = False
    ) -> Response:
        t_send = time.time()
        try:
            resp = await self.http.request(
                wire_req.method, wire_req.path, content=wire_req.body, headers=headers
            )
        except Exception as exc:  # noqa: BLE001
            t_end = time.time()
            ex = self._capture(
                client_id,
                tag,
                wire_req,
                norm_req,
                exchange_id=exchange_id,
                status=502,
                resp_headers={},
                streaming=False,
                body=b"",
                error={"type": type(exc).__name__, "message": str(exc)},
                t0=t0,
                t_send=t_send,
                t_first=None,
                t_end=t_end,
                is_replay=is_replay,
            )
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

        t_end = time.time()
        resp_body = resp.content
        wire_resp = WireResponse(
            status=resp.status_code,
            headers=dict(resp.headers),
            body=resp_body,
            body_json=_safe_json(resp_body),
        )
        try:
            norm_resp = self.out_adapter.parse_response(wire_resp)
        except Exception:
            norm_resp = None

        ex = self._capture(
            client_id,
            tag,
            wire_req,
            norm_req,
            exchange_id=exchange_id,
            status=resp.status_code,
            resp_headers=dict(resp.headers),
            streaming=False,
            body=resp_body,
            norm_resp=norm_resp,
            t0=t0,
            t_send=t_send,
            t_first=t_end,
            t_end=t_end,
            is_replay=is_replay,
        )
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
            content=resp_body,
            status_code=resp.status_code,
            headers=self._resp_headers(dict(resp.headers)),
            media_type=resp.headers.get("content-type"),
        )

    # ---- streaming: tap-and-forward with live UI fan-out ----
    async def _stream(
        self, wire_req, norm_req, client_id, tag, cid, exchange_id, headers, t0, *, is_replay: bool = False
    ):
        # Open the upstream stream *before* responding, so the client receives the
        # upstream's real status/headers (a clean 502 if it is unreachable) rather
        # than a premature 200 that is then aborted mid-body.
        t_send = time.time()
        # Hold the stream context manager alive across the response: httpx2's
        # stream() is an @asynccontextmanager, and dropping it would let it be
        # GC'd, closing the response stream before the body is read.
        stream_cm = self.http.stream(wire_req.method, wire_req.path, content=wire_req.body, headers=headers)
        try:
            up = await stream_cm.__aenter__()
        except Exception as exc:  # noqa: BLE001
            t_end = time.time()
            ex = self._capture(
                client_id,
                tag,
                wire_req,
                norm_req,
                exchange_id=exchange_id,
                status=502,
                resp_headers={},
                streaming=False,
                body=b"",
                error={"type": type(exc).__name__, "message": str(exc)},
                t0=t0,
                t_send=t_send,
                t_first=None,
                t_end=t_end,
                is_replay=is_replay,
            )
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

        status = up.status_code
        resp_headers = dict(up.headers)
        content_type = resp_headers.get("content-type", "")
        state: dict = {"first": None, "end": None, "first_content": None, "last_content": None}

        if "text/event-stream" in content_type:
            sse = SSEStream()
            raw_chunks: list[bytes] = []

            async def generate():
                try:
                    async for chunk in up.aiter_bytes():
                        if state["first"] is None:
                            state["first"] = time.time()
                        events = sse.feed(chunk)
                        for ev in events:
                            if ev["event"] == "message":
                                content, reasoning = _delta_parts(ev["data"])
                                if content or reasoning:
                                    now = time.time()
                                    if content:
                                        if state["first_content"] is None:
                                            state["first_content"] = now
                                        state["last_content"] = now
                                    self.hub.emit(
                                        cid,
                                        {
                                            "type": "delta",
                                            "conversation_id": cid,
                                            "exchange_id": exchange_id,
                                            "delta": content,
                                            "reasoning_delta": reasoning,
                                            "ts": now,
                                        },
                                    )
                        if self.settings.include_raw_chunks:
                            raw_chunks.append(chunk)
                        yield chunk
                finally:
                    state["end"] = time.time()
                    await stream_cm.__aexit__(None, None, None)
                    ex = self._capture(
                        client_id,
                        tag,
                        wire_req,
                        norm_req,
                        exchange_id=exchange_id,
                        status=status,
                        resp_headers=resp_headers,
                        streaming=True,
                        reassembled=sse.reassembled(),
                        raw_chunks=raw_chunks,
                        t0=t0,
                        t_send=t_send,
                        t_first=state["first"],
                        t_end=state["end"],
                        t_first_content=state["first_content"],
                        t_last_content=state["last_content"],
                        is_replay=is_replay,
                    )
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

        # Upstream returned a non-stream (e.g. a JSON error) for a stream request.
        data = b""
        try:
            async for chunk in up.aiter_bytes():
                if state["first"] is None:
                    state["first"] = time.time()
                data += chunk
        finally:
            state["end"] = time.time()
            await stream_cm.__aexit__(None, None, None)
        ex = self._capture(
            client_id,
            tag,
            wire_req,
            norm_req,
            exchange_id=exchange_id,
            status=status,
            resp_headers=resp_headers,
            streaming=False,
            body=data,
            t0=t0,
            t_send=t_send,
            t_first=state["first"],
            t_end=state["end"],
            is_replay=is_replay,
        )
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

    # ---- capture (build an Exchange and store it) ----
    def _capture(
        self,
        client_id,
        tag,
        wire_req,
        norm_req,
        *,
        exchange_id,
        status,
        resp_headers,
        streaming,
        body=None,
        reassembled=None,
        raw_chunks=None,
        norm_resp=None,
        error=None,
        t0,
        t_send,
        t_first,
        t_end,
        t_first_content=None,
        t_last_content=None,
        is_replay: bool = False,
    ) -> Exchange:
        body_bytes = body if body is not None else wire_req.body
        # Upstream-reported generation rate: llama.cpp embeds a ``timings`` object
        # (``predicted_n``/``predicted_ms``) in the final stream chunk and in
        # non-stream bodies. It is the only reliable tok/s for non-stream responses
        # and for reasoning models, where byte-level windows mislead.
        up_timings = None
        if reassembled is not None:
            up_timings = reassembled.get("timings")
        elif body_bytes:
            up_timings = (_safe_json(body_bytes) or {}).get("timings")
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
        if norm_resp is not None and norm_resp.usage is not None:
            u = norm_resp.usage
            usage = {
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "total_tokens": u.total_tokens,
            }
        elif reassembled and reassembled.get("usage"):
            u = reassembled["usage"]
            usage = {
                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                "completion_tokens": int(u.get("completion_tokens") or 0),
                "total_tokens": int(u.get("total_tokens") or 0),
            }

        server_response: dict = {
            "timestamp": t_first,
            "status": status,
            "headers": _redact(resp_headers),
            "streaming": streaming,
            "size_bytes": len(body_bytes) if body_bytes else 0,
        }
        if streaming and reassembled is not None:
            server_response["reassembled"] = reassembled
        else:
            server_response["body_json"] = _safe_json(body_bytes)
        if self.settings.include_raw_chunks and raw_chunks is not None:
            server_response["chunks"] = [c.decode("utf-8", errors="replace") for c in raw_chunks]

        exchange = Exchange(
            id=exchange_id,
            sequence=0,
            is_replay=is_replay,
            client_request={
                "timestamp": t0,
                "method": wire_req.method,
                "path": wire_req.path,
                "headers": _redact(wire_req.headers),
                "body_json": wire_req.body_json,
                "size_bytes": len(wire_req.body) if wire_req.body else 0,
            },
            server_response=server_response,
            timings=timings,
            usage=usage,
            error=error,
        )
        self.store.add_exchange(client_id, tag, exchange)
        return exchange
