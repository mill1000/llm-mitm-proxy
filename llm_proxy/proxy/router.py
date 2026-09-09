"""LLM listener router: a transparent catch-all.

Every request - any method, any path - is forwarded to the upstream verbatim
and tapped into the store. This listener has **no reserved routes** (the WebUI
and proxy API live on the UI port), so the proxy never 404s a client request;
at worst the upstream's own 404 is returned.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter()


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_all(request: Request) -> Response:
    pipeline = request.app.state.pipeline
    return await pipeline.handle(request)
