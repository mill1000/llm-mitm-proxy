"""Canonical, transport-agnostic representation (the "Normalized IR") plus raw wire objects.

This is the seam that lets in/out adapters be swapped independently later
(e.g. OpenAI in -> Anthropic out). Adapters translate wire <-> IR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: Any = None
    name: str | None = None
    tool_calls: list | None = None


@dataclass
class Choice:
    index: int = 0
    message: dict | None = None
    delta: dict | None = None
    finish_reason: str | None = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class NormalizedRequest:
    messages: list[Message]
    model: str
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    # adapter-specific passthrough params (re-emitted on serialize)
    extra: dict = field(default_factory=dict)


@dataclass
class NormalizedResponse:
    id: str
    model: str
    choices: list[Choice]
    usage: Usage | None = None
    # original parsed body (for the "full debug" view)
    raw: dict = field(default_factory=dict)


@dataclass
class WireRequest:
    method: str
    path: str
    headers: dict
    body: bytes
    body_json: dict | None = None


@dataclass
class WireResponse:
    status: int
    headers: dict
    body: bytes
    body_json: dict | None = None
    streaming: bool = False
    # for streams: reconstructed message/choices (see proxy/sse.py)
    reassembled: dict | None = None
