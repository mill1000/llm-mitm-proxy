"""FastAPI router for the proxy API (client -> proxy -> upstream).

Everything under ``/v1/*`` is proxied. Non-matching paths fall through to the
static UI mount (registered later in the app).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter()


@router.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy_v1(request: Request, path: str) -> Response:
    pipeline = request.app.state.pipeline
    return await pipeline.handle(request)
