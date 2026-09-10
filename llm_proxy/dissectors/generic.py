"""The generic dissector: the raw-capture fallback.

Matches anything that is not a chat completion. Everything is forwarded verbatim
and captured raw (wire method/path/status/size; the body as JSON when it parses).
"""

from __future__ import annotations

from ..model.ir import Delta, ParsedRequest, ParsedResponse, WireRequest
from .base import register


@register("generic")
class GenericDissector:
    name = "generic"

    def matches(self, method: str, path: str) -> bool:
        return True

    def request(self, wire: WireRequest) -> ParsedRequest:
        body = wire.body_json or {}
        return ParsedRequest(
            model=body.get("model"),
            body_json=wire.body_json,
            size_bytes=len(wire.body) if wire.body else 0,
        )

    def response_started(self, wire: WireRequest, status: int, headers: dict) -> None:
        return None

    def feed_chunk(self, handle, chunk: bytes) -> list[Delta]:
        return []

    def finalize(self, handle) -> ParsedResponse:
        return ParsedResponse()
