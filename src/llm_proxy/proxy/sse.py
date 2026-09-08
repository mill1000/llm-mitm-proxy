"""Minimal SSE line parser + reassembler.

Deliberately tiny: split on newlines, prefix-match ``data:``, parse JSON payloads,
and accumulate ``choices[].delta.content`` into a reassembled message. No heavy
dependency, no full-JSON parse per token (parse-once happens on the reassembled body).
"""

from __future__ import annotations

import json


class SSEStream:
    def __init__(self) -> None:
        self._buf = ""
        self.chunks: list[dict] = []
        self._content_parts: list[str] = []
        self._id = ""
        self._model = ""
        self._finish_reason: str | None = None
        self.usage: dict | None = None
        self.finished = False

    def feed(self, chunk: bytes) -> list[dict]:
        """Feed a raw (possibly partial) chunk. Returns any newly completed events."""
        self._buf += chunk.decode("utf-8", errors="replace")
        events: list[dict] = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    self.finished = True
                    events.append({"event": "done", "data": None})
                elif data:
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    events.append({"event": "message", "data": obj})
                    self._accumulate(obj)
        return events

    def _accumulate(self, obj: dict) -> None:
        self.chunks.append(obj)
        self._id = obj.get("id", self._id)
        self._model = obj.get("model", self._model)
        if obj.get("usage"):
            self.usage = obj.get("usage")
        for c in obj.get("choices", []) or []:
            delta = c.get("delta") or {}
            piece = delta.get("content")
            if piece:
                self._content_parts.append(piece)
            if c.get("finish_reason"):
                self._finish_reason = c.get("finish_reason")

    def reassembled(self) -> dict:
        """A reconstructed ``chat.completion`` object from the streamed deltas."""
        return {
            "id": self._id,
            "object": "chat.completion",
            "model": self._model,
            "stream": True,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(self._content_parts)},
                    "finish_reason": self._finish_reason,
                }
            ],
            "usage": self.usage,
        }
