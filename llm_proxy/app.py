"""FastAPI app factory. One process serves: proxy API, UI REST API, and the static UI.

Run with: ``llm-proxy`` (console script; command-line settings, see --help) or
``uvicorn llm_proxy.app:app`` for ad-hoc runs with default settings. The live
WebSocket hub + in-memory store live in this single process.
"""

from __future__ import annotations

import argparse
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import __version__
from .adapters.base import AdapterRegistry
from .api.ui import router as ui_router
from .config import Settings
from .hub import Hub
from .logconf import configure_logging
from .proxy.pipeline import Pipeline
from .proxy.router import router as proxy_router
from .store.memory import MemoryStore


def _ui_dir(settings: Settings) -> Path:
    """Resolve the static UI directory (``UI_DIR`` override, else repo-relative ``ui/``)."""
    if settings.ui_dir:
        return Path(settings.ui_dir)
    return Path(__file__).resolve().parents[1] / "ui"


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
    settings = app.state.settings

    # Wire the LOG_LEVEL setting to the app loggers. uvicorn's
    # dictConfig leaves the root logger without a handler, so without this our
    # INFO logs (e.g. the llm_proxy.ws connection trace) would be silently dropped.
    configure_logging(settings)

    # Pooled, keep-alive async client to the single upstream. The read timeout is
    # the max gap between upstream bytes: long enough for slow generation, short
    # enough to trip on a dead link. See UPSTREAM_*_TIMEOUT.
    limits = httpx2.Limits(max_connections=200, max_keepalive_connections=50)
    timeout = httpx2.Timeout(
        settings.upstream_read_timeout,
        connect=settings.upstream_connect_timeout,
        pool=settings.upstream_pool_timeout,
    )
    app.state.http = httpx2.AsyncClient(base_url=settings.upstream_base_url, limits=limits, timeout=timeout)
    app.state.store = MemoryStore(settings.retention_max_exchanges, settings.retention_max_age_hours)
    app.state.pipeline = Pipeline(app.state.http, settings, AdapterRegistry(), app.state.store, app.state.hub)
    try:
        yield
    finally:
        await app.state.http.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (default settings when none are given)."""
    settings = settings or Settings()
    app = FastAPI(title="LLM Proxy", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.hub = Hub(
        ping_interval=settings.ws_ping_interval,
        ping_timeout=settings.ws_ping_timeout,
    )

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
                elif mtype == "pong":
                    hub.pong(ws)
        except WebSocketDisconnect as exc:
            log.info("ws closed by client %s: code=%s reason=%s", ws.client, exc.code, exc.reason or "-")
        finally:
            hub.disconnect(ws)

    # Static UI (served last, so /v1/*, /api/*, /health win).
    ui_dir = _ui_dir(settings)

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


def cli_overrides(argv: list[str] | None = None) -> dict[str, str | int]:
    """Parse the command line into settings overrides.

    Usage: ``llm-proxy [UPSTREAM_BASE_URL] [--host HOST] [--port PORT]``.
    Returns only the options actually given, keyed by ``Settings`` field name,
    so the result can be passed straight to ``Settings(**overrides)``.
    """
    parser = argparse.ArgumentParser(
        prog="llm-proxy",
        description="OpenAI-compatible LLM proxy with a live conversation WebUI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("upstream", nargs="?", metavar="UPSTREAM_BASE_URL", help="upstream base URL")
    parser.add_argument("--host", metavar="HOST", help="listen host")
    parser.add_argument("--port", metavar="PORT", type=int, help="listen port")
    parser.add_argument("--upstream-api-key", metavar="KEY", help="server-side fallback API key")
    parser.add_argument("--log-level", metavar="LEVEL", help="app log level")
    parser.add_argument("--ui-dir", metavar="DIR", help="static UI directory")
    args = parser.parse_args(argv)
    opts = {
        "upstream_base_url": args.upstream,
        "listen_host": args.host,
        "listen_port": args.port,
        "upstream_api_key": args.upstream_api_key,
        "log_level": args.log_level,
        "ui_dir": args.ui_dir,
    }
    return {key: value for key, value in opts.items() if value is not None}


def main() -> None:
    """Console entry point (``llm-proxy``): command-line settings over defaults."""
    settings = Settings(**cli_overrides())
    uvicorn.run(create_app(settings), host=settings.listen_host, port=settings.listen_port)
