"""Dissector protocol + registry.

Dissectors are observation-only: they extract request/response data for the UI
and store, and never touch the wire (the pipeline forwards everything verbatim).
Selection is per request by method+path; a request that is not a chat completion
falls back to the generic raw-capture dissector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..model.ir import Delta, ParsedRequest, ParsedResponse, WireRequest


class Dissector(Protocol):
    """Observes one proxied request/response and decodes what it recognizes."""

    name: str

    def matches(self, method: str, path: str) -> bool:
        """Whether this dissector recognizes the request (method + path)."""
        ...

    def request(self, wire: WireRequest) -> ParsedRequest:
        """Extract display data from the client request."""
        ...

    def response_started(self, wire: WireRequest, status: int, headers: dict) -> object:
        """Open a per-response parse handle from the upstream response head.

        Returns ``None`` when there is nothing to feed: the pipeline still
        forwards the body and captures it raw.
        """
        ...

    def feed_chunk(self, handle: object, chunk: bytes) -> list[Delta]:
        """Feed one upstream body chunk; return any decoded deltas (live UI)."""
        ...

    def finalize(self, handle: object) -> ParsedResponse:
        """Close the handle and return what the response decoded to."""
        ...


@dataclass
class NonStream:
    """A plain byte accumulator for responses that are not stream-parsed."""

    body: bytes = b""

    def feed(self, chunk: bytes) -> None:
        self.body += chunk


_REGISTRY: dict[str, type] = {}


def register(name: str):
    def deco(cls):
        _REGISTRY[name] = cls
        return cls

    return deco


def get_dissector(name: str):
    if name not in _REGISTRY:
        raise KeyError(f"unknown dissector: {name!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def available() -> list[str]:
    return sorted(_REGISTRY)
