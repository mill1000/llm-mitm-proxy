"""FastAPI app factory. One process serves: proxy API, UI REST API, and the static UI.

Run with: ``uvicorn llm_proxy.app:app --host 0.0.0.0 --port 9090 --workers 1``
(live WebSocket hub + in-memory store live in this single process).
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import __version__
from .adapters.base import AdapterRegistry
from .api.ui import router as ui_router
from .config import Settings, get_settings
from .hub import Hub
from .proxy.pipeline import Pipeline
from .proxy.router import router as proxy_router
from .store.memory import MemoryStore


def _ui_dir(settings: Settings) -> Path:
    """Resolve the static UI directory (``UI_DIR`` override, else package-relative ``ui/``)."""
    if settings.ui_dir:
        return Path(settings.ui_dir)
    return Path(__file__).resolve().parents[2] / "ui"


class _NoCacheStaticFiles(StaticFiles):
    """UI assets always revalidate.

    Without Cache-Control, browsers heuristically cache app.js/styles.css. A
    proxy restart can then leave a tab running stale UI code against the new
    server process (e.g. a live WS but old dock logic), which is confusing to
    diagnose. no-cache costs one conditional request per asset per reload.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Wire the LOG_LEVEL setting to the app loggers. uvicorn's dictConfig leaves
    # the root logger without a handler, so without this our INFO logs (e.g. the
    # llm_proxy.ws connection trace) would be silently dropped.
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Pooled, keep-alive async client to the single upstream. Long read timeout for LLM generation.
    limits = httpx2.Limits(max_connections=200, max_keepalive_connections=50)
    timeout = httpx2.Timeout(300.0, connect=10.0)
    app.state.http = httpx2.AsyncClient(base_url=settings.upstream_base_url, limits=limits, timeout=timeout)
    app.state.settings = settings
    app.state.store = MemoryStore(settings.retention_max_exchanges, settings.retention_max_age_hours)
    app.state.pipeline = Pipeline(app.state.http, settings, AdapterRegistry(), app.state.store, app.state.hub)
    try:
        yield
    finally:
        await app.state.http.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="LLM Proxy", version=__version__, lifespan=lifespan)
    app.state.hub = Hub()

    # Proxy API + UI REST API first so they take precedence over the static mount.
    app.include_router(proxy_router)
    app.include_router(ui_router, prefix="/api")

    @app.get("/health")
    async def health() -> dict:
        # Probe the upstream with a short timeout so the UI pill reflects the *real*
        # link state: the proxy process being alive alone is not useful here.
        upstream = "ok"
        try:
            r = await app.state.http.get("/v1/models", timeout=3.0)
            if r.status_code >= 400:
                upstream = f"error ({r.status_code})"
        except Exception:  # noqa: BLE001
            upstream = "error"
        return {"status": "ok", "upstream": upstream}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        log = logging.getLogger("llm_proxy.ws")
        hub = app.state.hub
        await hub.connect(ws)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "subscribe":
                    hub.set_focus(ws, msg.get("conversation_id"))
                elif mtype == "unsub":
                    hub.set_focus(ws, None)
        except WebSocketDisconnect as exc:
            log.info("ws closed by client %s: code=%s reason=%s", ws.client, exc.code, exc.reason or "-")
        finally:
            hub.disconnect(ws)

    # Static UI (served last, so /v1/*, /api/*, /health win).
    ui_dir = _ui_dir(get_settings())

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        # Browsers probe /favicon.ico even when the page links an SVG icon; serve the SVG to keep logs clean.
        svg = ui_dir / "favicon.svg"
        if not svg.is_file():
            raise HTTPException(status_code=404, detail="favicon not found")
        return Response(content=svg.read_bytes(), media_type="image/svg+xml")

    if ui_dir.is_dir():
        app.mount("/", _NoCacheStaticFiles(directory=ui_dir, html=True), name="ui")

    return app


app = create_app()
