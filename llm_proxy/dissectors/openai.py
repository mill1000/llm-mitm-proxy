"""The OpenAI-compatible chat decoder: decodes ``POST */chat/completions``.

The only request shape the proxy decodes - llama.cpp speaks OpenAI, so this one
decoder covers both pure-OpenAI and llama.cpp clients. Request: model, stream
flag, a preview of the last user message, and the messages list. Response: live
``choices[].delta`` streaming (content + reasoning + tool_calls) reassembled
into a chat completion, plus usage and upstream timings. Anything that is not a
chat completion is handled by the generic raw-capture fallback.
"""

from __future__ import annotations

from ..model.ir import Delta, ParsedRequest, ParsedResponse, WireRequest
from ..proxy.sse import SSEStream
from ..proxy.util import safe_json
from .base import NonStream, register
from .generic import GenericDissector


def _last_user_preview(body_json: dict | None, limit: int = 500) -> str | None:
    """A short, UI-friendly preview of the last user message (no full body over WS).

    Truncation is always marked with a trailing ellipsis.
    """
    if not body_json:
        return None
    for m in reversed(body_json.get("messages", []) or []):
        if m.get("role") == "user":
            c = m.get("content")
            if not c:
                continue
            s = c if isinstance(c, str) else str(c)
            return s if len(s) <= limit else s[:limit] + "…"
    return None


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


def _dict_or_none(v) -> dict | None:
    return v if isinstance(v, dict) else None


@register("openai")
class OpenAIDissector(GenericDissector):
    name = "openai"

    def matches(self, method: str, path: str) -> bool:
        # OpenAI-compatible chat: the one request shape we decode. llama.cpp
        # speaks it too, so this covers both pure-OpenAI and llama.cpp clients.
        return method == "POST" and path.split("?", 1)[0].endswith("/chat/completions")

    def request(self, wire: WireRequest) -> ParsedRequest:
        body = wire.body_json or {}
        return ParsedRequest(
            model=body.get("model"),
            stream=bool(body.get("stream")),
            preview=_last_user_preview(wire.body_json),
            messages=body.get("messages"),
            body_json=wire.body_json,
            size_bytes=len(wire.body) if wire.body else 0,
        )

    def response_started(self, wire: WireRequest, status: int, headers: dict):
        if "text/event-stream" in headers.get("content-type", ""):
            return SSEStream()
        return NonStream()

    def feed_chunk(self, handle, chunk: bytes) -> list[Delta]:
        if isinstance(handle, NonStream):
            handle.feed(chunk)
            return []
        if handle is None:
            return []
        out: list[Delta] = []
        for ev in handle.feed(chunk):
            if ev["event"] == "message":
                content, reasoning = _delta_parts(ev["data"])
                if content or reasoning:
                    out.append(Delta(delta=content, reasoning_delta=reasoning))
        return out

    def finalize(self, handle) -> ParsedResponse:
        if handle is None:
            return ParsedResponse()
        if isinstance(handle, NonStream):
            body_json = safe_json(handle.body)
            if body_json is None:
                return ParsedResponse()
            return ParsedResponse(
                body_json=body_json,
                usage=_dict_or_none(body_json.get("usage")),
                timings=_dict_or_none(body_json.get("timings")),
            )
        return ParsedResponse(reassembled=handle.reassembled(), usage=handle.usage, timings=handle.timings)
