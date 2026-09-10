"""Wire objects + the dissector parse results (observation-only).

Dissectors produce ParsedRequest/ParsedResponse for the UI and store; the wire
bytes themselves always pass through the proxy untouched.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WireRequest:
    method: str
    path: str
    headers: dict
    body: bytes
    body_json: dict | None = None


@dataclass
class ParsedRequest:
    """What a dissector extracted from a client request (display data only)."""

    model: str | None = None
    stream: bool = False
    preview: str | None = None
    messages: list | None = None
    body_json: dict | None = None
    size_bytes: int = 0


@dataclass
class ParsedResponse:
    """What a dissector decoded from an upstream response (display data only)."""

    reassembled: dict | None = None
    usage: dict | None = None
    timings: dict | None = None
    body_json: dict | None = None


@dataclass
class Delta:
    """One decoded streaming delta (content and/or reasoning text)."""

    delta: str = ""
    reasoning_delta: str = ""
