"""The proxy pipeline: identify client, forward to upstream, tap the response, capture.

Core behavior (v1 same-format OpenAI -> OpenAI) is **tap-and-forward**: the upstream
response (including SSE streams) is streamed back to the client untouched, while a
side parse accumulates the exchange for the store. The tap never blocks the client path.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx2
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from ..adapters.base import AdapterRegistry
from ..model.conversation import Exchange
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


class Pipeline:
    def __init__(self, http: httpx2.AsyncClient, settings, registry: AdapterRegistry, store: MemoryStore):
        self.http = http
        self.settings = settings
        self.store = store
        self.in_adapter = registry.in_adapter(settings.in_adapter)
        self.out_adapter = registry.out_adapter(settings.out_adapter)
        self.same_format = settings.in_adapter == settings.out_adapter

    # ---- client identity ----
    @staticmethod
    def _extract_key(headers: dict) -> str | None:
        auth = headers.get("authorization") or headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
        return None

    def client_id(self, request: Request) -> str:
        if self.settings.client_id_header:
            override = request.headers.get(self.settings.client_id_header.lower())
            if override:
                return override
        host = request.client.host if request.client else "unknown"
        key = self._extract_key(dict(request.headers))
        return f"{host}::{key}" if key else host

    # ---- upstream request shaping (transparent passthrough + key fallback) ----
    def _upstream_headers(self, headers: dict) -> dict:
        out = {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}
        if not any(k.lower() == "authorization" for k in out) and self.settings.upstream_api_key:
            out["authorization"] = f"Bearer {self.settings.upstream_api_key}"
        return out

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

        is_stream = bool(wire_req.body_json and wire_req.body_json.get("stream"))
        if is_stream:
            return await self._stream(wire_req, norm_req, client_id, tag, headers, t0)
        return await self._non_stream(wire_req, norm_req, client_id, tag, headers, t0)

    # ---- non-streaming ----
    async def _non_stream(self, wire_req, norm_req, client_id, tag, headers, t0) -> Response:
        t_send = time.time()
        try:
            resp = await self.http.request(
                wire_req.method, wire_req.path, content=wire_req.body, headers=headers
            )
        except Exception as exc:  # noqa: BLE001
            t_end = time.time()
            self._capture(
                client_id,
                tag,
                wire_req,
                norm_req,
                status=502,
                resp_headers={},
                streaming=False,
                body=b"",
                error={"type": type(exc).__name__, "message": str(exc)},
                t0=t0,
                t_send=t_send,
                t_first=None,
                t_end=t_end,
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

        self._capture(
            client_id,
            tag,
            wire_req,
            norm_req,
            status=resp.status_code,
            resp_headers=dict(resp.headers),
            streaming=False,
            body=resp_body,
            norm_resp=norm_resp,
            t0=t0,
            t_send=t_send,
            t_first=t_end,
            t_end=t_end,
        )
        return Response(
            content=resp_body,
            status_code=resp.status_code,
            headers=self._resp_headers(dict(resp.headers)),
            media_type=resp.headers.get("content-type"),
        )

    # ---- streaming: tap-and-forward (live UI fan-out lands in M1) ----
    async def _stream(self, wire_req, norm_req, client_id, tag, headers, t0):
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
            self._capture(
                client_id,
                tag,
                wire_req,
                norm_req,
                status=502,
                resp_headers={},
                streaming=False,
                body=b"",
                error={"type": type(exc).__name__, "message": str(exc)},
                t0=t0,
                t_send=t_send,
                t_first=None,
                t_end=t_end,
            )
            return Response(
                content=json.dumps({"error": {"message": f"upstream error: {exc}"}}).encode(),
                status_code=502,
                media_type="application/json",
            )

        status = up.status_code
        resp_headers = dict(up.headers)
        content_type = resp_headers.get("content-type", "")
        state: dict = {"first": None, "end": None}

        if "text/event-stream" in content_type:
            sse = SSEStream()
            raw_chunks: list[bytes] = []

            async def generate():
                try:
                    async for chunk in up.aiter_bytes():
                        if state["first"] is None:
                            state["first"] = time.time()
                        sse.feed(chunk)
                        if self.settings.include_raw_chunks:
                            raw_chunks.append(chunk)
                        yield chunk
                finally:
                    state["end"] = time.time()
                    await stream_cm.__aexit__(None, None, None)
                    self._capture(
                        client_id,
                        tag,
                        wire_req,
                        norm_req,
                        status=status,
                        resp_headers=resp_headers,
                        streaming=True,
                        reassembled=sse.reassembled(),
                        raw_chunks=raw_chunks,
                        t0=t0,
                        t_send=t_send,
                        t_first=state["first"],
                        t_end=state["end"],
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
        self._capture(
            client_id,
            tag,
            wire_req,
            norm_req,
            status=status,
            resp_headers=resp_headers,
            streaming=False,
            body=data,
            t0=t0,
            t_send=t_send,
            t_first=state["first"],
            t_end=state["end"],
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
    ) -> None:
        body_bytes = body if body is not None else wire_req.body
        timings = {
            "t_request_in": t0,
            "t_upstream_send": t_send,
            "t_first_byte": t_first,
            "t_end": t_end,
            "ttft_ms": round((t_first - t_send) * 1000, 2) if (t_first and t_send) else None,
            "total_ms": round((t_end - t0) * 1000, 2) if (t_end and t0) else None,
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
            id=str(uuid.uuid4()),
            sequence=0,
            is_replay=False,
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
