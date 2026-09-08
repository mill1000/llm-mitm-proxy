"""A tiny in-process stand-in for a local llama.cpp ``/v1`` server."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

mock = FastAPI()


@mock.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": "local-model", "object": "model"}]}


@mock.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    stream = bool(body.get("stream", False))
    model = body.get("model", "local-model")
    if model == "boom":
        return JSONResponse({"error": {"message": "model not found"}}, status_code=404)

    async def gen():
        for piece in ["Hello", ", ", "world", "!"]:
            chunk = {
                "id": "c1",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0.01)
        final = {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"

    if stream:
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse(
        {
            "id": "c1",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello, world!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
    )


_server: uvicorn.Server | None = None
_thread: threading.Thread | None = None


def start_mock(port: int = 8082) -> None:
    """Start the mock upstream on ``127.0.0.1:port`` and block until it accepts."""
    global _server, _thread
    config = uvicorn.Config(mock, host="127.0.0.1", port=port, log_level="error")
    _server = uvicorn.Server(config)
    _thread = threading.Thread(target=_server.run, daemon=True)
    _thread.start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=0.2)
            return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("mock upstream did not start")


def stop_mock() -> None:
    """Signal the mock upstream to shut down (best-effort)."""
    global _server, _thread
    if _server is not None:
        _server.should_exit = True
        if _thread is not None:
            _thread.join(timeout=3)
    _server = None
    _thread = None
