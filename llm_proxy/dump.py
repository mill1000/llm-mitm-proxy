"""Conversation dump / export (versioned JSON). Format described in PLAN.md section 7."""

from __future__ import annotations

import time

from . import __version__
from .model.conversation import Conversation


def _iso(ts: float) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def _stats(exchanges) -> dict:
    n = len(exchanges)
    prompt_tokens = sum(int((e.usage or {}).get("prompt_tokens") or 0) for e in exchanges)
    completion_tokens = sum(int((e.usage or {}).get("completion_tokens") or 0) for e in exchanges)
    ttfts = [e.timings["ttft_ms"] for e in exchanges if e.timings and e.timings.get("ttft_ms") is not None]
    totals = [e.timings["total_ms"] for e in exchanges if e.timings and e.timings.get("total_ms") is not None]
    return {
        "exchange_count": n,
        "total_prompt_tokens": prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "avg_ttft_ms": round(sum(ttfts) / len(ttfts), 2) if ttfts else None,
        "avg_total_ms": round(sum(totals) / len(totals), 2) if totals else None,
    }


def _proxy_block(settings) -> dict:
    return {
        "version": __version__,
        "in_adapter": settings.in_adapter,
        "out_adapter": settings.out_adapter,
        "upstream": {
            "name": "llama.cpp",
            "base_url": settings.upstream_base_url,
            "model": None,
        },
    }


def conversation_to_dump(conv: Conversation, settings) -> dict:
    exchanges = conv.exchanges
    return {
        "format": "llm-proxy/conversation",
        "version": 1,
        "exported_at": _iso(time.time()),
        "proxy": _proxy_block(settings),
        "client": {"id": conv.client_id, "name": conv.client_id},
        "stats": _stats(exchanges),
        "exchanges": [ex.to_dict() for ex in exchanges],
    }


def exchange_to_dump(exchange, settings) -> dict:
    """A single exchange, for re-importing or offline inspection of one request/response."""
    return {
        "format": "llm-proxy/exchange",
        "version": 1,
        "exported_at": _iso(time.time()),
        "proxy": _proxy_block(settings),
        "exchange": exchange.to_dict(),
    }
