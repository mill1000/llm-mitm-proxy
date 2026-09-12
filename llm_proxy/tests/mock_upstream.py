"""A tiny in-process stand-in for a local llama.cpp ``/v1`` server."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)

mock = FastAPI()

# llama.cpp embeds generation timings in the final stream chunk / non-stream body;
# 3 tokens in 30ms -> 100 tok/s. Lets tests assert the proxy surfaces them.
TIMINGS = {"prompt_n": 45, "prompt_ms": 1150.0, "predicted_n": 3, "predicted_ms": 30.0}

# Bodies of every chat request received, in order. Lets tests assert replay
# re-sends the captured request (or the replacing body) verbatim to the upstream.
REQUESTS: list[dict] = []


@mock.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": "local-model", "object": "model"}]}


@mock.get("/props")
async def props(request: Request) -> dict:
    """llama.cpp server-properties probe (a non-OpenAI path the proxy must pass through)."""
    return {"n_ctx": 4096, "model": request.query_params.get("model", "")}


@mock.get("/plain")
async def plain() -> PlainTextResponse:
    """A non-JSON response body (exercises the raw-text capture path)."""
    return PlainTextResponse("hello")


# A tiny 1x1 transparent PNG (magic bytes + NULs). Exercises the binary-response
# capture path: the proxy must forward it verbatim and record its content-type
# and size rather than decoding it to garbled text.
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd4000000004945"
    "4e44ae426082"
)


@mock.get("/img")
async def img() -> Response:
    """A binary (image/png) response body (a proxied non-LLM web-UI asset)."""
    return Response(content=PNG_1x1, media_type="image/png")


# Small text assets of a proxied non-LLM web UI. These must be captured
# opaquely (content-type + size), not decoded into the store.
@mock.get("/style.css")
async def style_css() -> Response:
    return Response(content=b"body{color:red;font:12px sans-serif}", media_type="text/css")


@mock.get("/app.js")
async def app_js() -> Response:
    return Response(content=b"console.log('asset')", media_type="application/javascript")


@mock.get("/index.html")
async def index_html() -> Response:
    return Response(content=b"<html><body><h1>ui</h1></body></html>", media_type="text/html")


@mock.get("/icon.svg")
async def icon_svg() -> Response:
    return Response(content=b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', media_type="image/svg+xml")


@mock.post("/blob")
async def blob(request: Request) -> Response:
    """Echoes a binary request body back (exercises binary request AND response
    capture: the proxy records content-type + size, never garbled text)."""
    data = await request.body()
    return Response(content=data, media_type=request.headers.get("content-type", "application/octet-stream"))


@mock.get("/models/sse")
async def models_sse() -> StreamingResponse:
    """llama.cpp's model-load progress stream: a non-chat SSE (raw capture, never
    reassembled as a chat completion)."""

    async def gen():
        for i in range(3):
            yield f"data: {json.dumps({'status': 'loading', 'i': i})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@mock.get("/sse")
async def sse() -> StreamingResponse:
    """A non-OpenAI SSE stream (exercises the undecoded-SSE raw capture path)."""

    async def gen():
        for i in range(3):
            yield f"data: {json.dumps({'event': 'tick', 'i': i})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _sse_line(
    model: str, delta: dict, finish: str | None = None, usage: dict | None = None, timings: dict | None = None
) -> str:
    obj: dict = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        obj["usage"] = usage
    if timings:
        obj["timings"] = timings
    return f"data: {json.dumps(obj)}\n\n"


@mock.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    REQUESTS.append(body)
    stream = bool(body.get("stream", False))
    model = body.get("model", "local-model")
    if model == "boom":
        return JSONResponse({"error": {"message": "model not found"}}, status_code=404)

    # "thinker" mirrors real reasoning models (e.g. llama.cpp --reasoning-preserve):
    # thinking streams first in delta.reasoning_content, content afterwards.
    if stream and model == "thinker":

        async def gen_think():
            for piece in ["Let", " me", " think", "..."]:
                yield _sse_line(model, {"reasoning_content": piece})
                await asyncio.sleep(0.005)
            for piece in ["Hi", "!"]:
                yield _sse_line(model, {"content": piece})
                await asyncio.sleep(0.005)
            yield _sse_line(
                model,
                {},
                finish="stop",
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                timings={"prompt_n": 4, "prompt_ms": 100.0, "predicted_n": 2, "predicted_ms": 10.0},
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen_think(), media_type="text/event-stream")

    if stream and model == "tooler":
        # Agentic reply: content plus fragmented delta.tool_calls (id/name in
        # the first fragment, arguments appended across later ones).

        async def gen_tool():
            yield _sse_line(model, {"content": "Let me check."})
            yield _sse_line(
                model,
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": ""},
                        }
                    ]
                },
            )
            for frag in ('{"com', 'mand": "ls"}'):
                yield _sse_line(model, {"tool_calls": [{"index": 0, "function": {"arguments": frag}}]})
                await asyncio.sleep(0.005)
            yield _sse_line(
                model,
                {},
                finish="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                timings=TIMINGS,
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen_tool(), media_type="text/event-stream")

    if model == "slow":
        # Holds the request open so tests can observe the in-flight exchange in REST.
        await asyncio.sleep(0.8)

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
            "timings": TIMINGS,
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
            "timings": TIMINGS,
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
