"""OpenAI-protocol adapter (v1: both in and out).

``build_request`` / ``serialize_response`` are implemented structurally so a
cross-format path (in != out) can be enabled later without touching the core.
For the v1 same-format case the pipeline forwards the original bytes instead.
"""

from __future__ import annotations

import json

from ..model.ir import (
    Choice,
    Message,
    NormalizedRequest,
    NormalizedResponse,
    Usage,
    WireRequest,
    WireResponse,
)
from .base import register_in, register_out


def _parse_messages(body: dict) -> list[Message]:
    out: list[Message] = []
    for m in body.get("messages", []) or []:
        out.append(
            Message(
                role=m.get("role"),
                content=m.get("content"),
                name=m.get("name"),
                tool_calls=m.get("tool_calls"),
            )
        )
    return out


def _to_choices(body: dict) -> list[Choice]:
    return [
        Choice(
            index=c.get("index", 0),
            message=c.get("message"),
            delta=c.get("delta"),
            finish_reason=c.get("finish_reason"),
        )
        for c in body.get("choices", []) or []
    ]


def _usage_from(body: dict) -> Usage | None:
    u = body.get("usage")
    if not u:
        return None
    return Usage(
        prompt_tokens=u.get("prompt_tokens", 0),
        completion_tokens=u.get("completion_tokens", 0),
        total_tokens=u.get("total_tokens", 0),
    )


@register_in("openai")
@register_out("openai")
class OpenAIAdapter:
    name = "openai"
    supported_paths = ["/v1/chat/completions", "/v1/completions", "/v1/embeddings"]

    # ---- In: speaks to the client ----
    def parse_request(self, wire: WireRequest) -> NormalizedRequest:
        body = wire.body_json or {}
        known = {"messages", "model", "stream", "temperature", "max_tokens", "top_p", "stop"}
        return NormalizedRequest(
            messages=_parse_messages(body),
            model=body.get("model", ""),
            stream=bool(body.get("stream", False)),
            temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"),
            top_p=body.get("top_p"),
            stop=body.get("stop"),
            extra={k: v for k, v in body.items() if k not in known},
        )

    def serialize_response(self, norm: NormalizedResponse, wire_ctx=None) -> bytes:
        # Same-format passthrough: return the original upstream bytes when available.
        if wire_ctx and wire_ctx.get("original_body"):
            return wire_ctx["original_body"]
        obj: dict = {
            "id": norm.id,
            "object": "chat.completion",
            "model": norm.model,
            "choices": [
                {"index": c.index, "message": c.message, "finish_reason": c.finish_reason}
                for c in norm.choices
            ],
        }
        if norm.usage is not None:
            obj["usage"] = {
                "prompt_tokens": norm.usage.prompt_tokens,
                "completion_tokens": norm.usage.completion_tokens,
                "total_tokens": norm.usage.total_tokens,
            }
        return json.dumps(obj).encode()

    # ---- Out: speaks to the upstream ----
    def build_request(self, norm: NormalizedRequest) -> WireRequest:
        body: dict = {
            "model": norm.model,
            "stream": norm.stream,
            "messages": [
                {
                    k: v
                    for k, v in {
                        "role": m.role,
                        "content": m.content,
                        "name": m.name,
                        "tool_calls": m.tool_calls,
                    }.items()
                    if v is not None
                }
                for m in norm.messages
            ],
        }
        for key in ("temperature", "max_tokens", "top_p", "stop"):
            val = getattr(norm, key)
            if val is not None:
                body[key] = val
        body.update(norm.extra)
        raw = json.dumps(body).encode()
        return WireRequest(
            method="POST",
            path="/v1/chat/completions",
            headers={"content-type": "application/json"},
            body=raw,
            body_json=body,
        )

    def parse_response(self, wire: WireResponse) -> NormalizedResponse:
        body = wire.body_json or {}
        return NormalizedResponse(
            id=body.get("id", ""),
            model=body.get("model", ""),
            choices=_to_choices(body),
            usage=_usage_from(body),
            raw=body,
        )
