"""FastAPI app factory: one process, two listeners.

* **LLM listener** (``llm_port``, default 8080): a transparent catch-all proxy
  to the upstream. Every method/path is forwarded verbatim and tapped.
* **UI listener** (``ui_port``, default 9090): the WebUI, ``/api/*`` REST, the
  ``/ws`` WebSocket, and ``/health``. It serves no proxy routes.

Run with ``llm-proxy`` (console script; both listeners, command-line settings,
see --help) or ``uvicorn llm_proxy.app:app --timeout-graceful-shutdown 2
--log-level warning --no-access-log`` for an ad-hoc **UI-only** run with
default settings. The store and WebSocket hub live in this single process and are
shared by both listeners; each listener builds its own upstream http client +
pipeline in its lifespan (one connection pool per listener).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
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
from .api.ui import router as ui_router
from .config import Settings
from .hub import Hub
from .logconf import configure_logging
from .proxy.pipeline import Pipeline
from .proxy.router import router as llm_router
from .store.memory import MemoryStore

log = logging.getLogger("llm_proxy")


def _ui_dir(settings: Settings) -> Path:
    """Resolve the static UI directory (``ui_dir`` override, else repo-relative ``ui/``)."""
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


class Context:
    """Shared, loop-agnostic process state: settings, store, and WebSocket hub.

    Both listeners run in one process and must share the store (captured
    exchanges) and the hub (live fan-out) - that is the point of a single
    process. Each listener separately builds its own upstream http client +
    pipeline (see :meth:`build_pipeline`) so it runs in its own event-loop
    context; in production both listeners share one loop, so this costs a second
    (small) connection pool only.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        # Wire the log level to the root logger (idempotent). In the ad-hoc
        # ``uvicorn llm_proxy.app:app`` path, uvicorn's dictConfig leaves root
        # without a handler, so without this the app's logs would be dropped.
        configure_logging(settings)
        self.store = MemoryStore(settings.retention_max_exchanges, settings.retention_max_age_hours)
        self.hub = Hub(
            ping_interval=settings.ws_ping_interval,
            ping_timeout=settings.ws_ping_timeout,
        )

    def build_pipeline(self) -> tuple[httpx2.AsyncClient, Pipeline]:
        """A fresh (upstream http client, pipeline) pair for one listener.

        Pooled, keep-alive client to the single upstream. No read timeout: the
        proxy is transparent, so timeout policy belongs to the client (a gap cap
        would kill silent persistent streams like /models/sse). The connect
        timeout is the dead-link detector.
        """
        limits = httpx2.Limits(max_connections=200, max_keepalive_connections=50)
        timeout = httpx2.Timeout(
            None,
            connect=self.settings.upstream_connect_timeout,
            pool=self.settings.upstream_pool_timeout,
        )
        http = httpx2.AsyncClient(base_url=self.settings.upstream_base_url, limits=limits, timeout=timeout)
        pipeline = Pipeline(http, self.settings, self.store, self.hub)
        return http, pipeline


def _attach(ctx: Context, app: FastAPI) -> None:
    """Expose the shared Context (settings/store/hub) on ``app.state``.

    ``http`` and ``pipeline`` are added later, by the listener's lifespan, once
    its event loop is running.
    """
    app.state.ctx = ctx
    app.state.settings = ctx.settings
    app.state.hub = ctx.hub
    app.state.store = ctx.store


def _lifespan(ctx: Context):
    """Build this listener's http client + pipeline on startup, close on shutdown."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http, pipeline = ctx.build_pipeline()
        app.state.http = http
        app.state.pipeline = pipeline
        try:
            yield
        finally:
            await http.aclose()

    return lifespan


def create_llm_app(ctx: Context) -> FastAPI:
    """LLM listener: a transparent catch-all proxy (no reserved routes)."""
    app = FastAPI(
        title="LLM Proxy",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan(ctx),
    )
    _attach(ctx, app)
    app.include_router(llm_router)
    return app


def create_ui_app(ctx: Context) -> FastAPI:
    """UI listener: WebUI + ``/api/*`` + ``/ws`` + ``/health`` (no proxy routes)."""
    app = FastAPI(
        title="LLM Proxy UI",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan(ctx),
    )
    _attach(ctx, app)
    app.include_router(ui_router, prefix="/api")

    @app.get("/health")
    async def health() -> dict:
        # Reachability only, not an OpenAI probe: any HTTP response (even a 404)
        # means the upstream is reachable; only a connect failure/timeout means it
        # is down. Protocol-agnostic - no assumption about which API it speaks.
        upstream = "ok"
        try:
            await app.state.http.get("/", timeout=3.0)
        except Exception:  # noqa: BLE001
            upstream = "error"
        return {
            "status": "ok",
            "upstream": upstream,
            "upstream_url": app.state.settings.upstream_base_url,
        }

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
            log.debug("ws closed by client %s: code=%s reason=%s", ws.client, exc.code, exc.reason or "-")
        finally:
            hub.disconnect(ws)

    # Static UI (served last, so /api/*, /health win).
    ui_dir = _ui_dir(ctx.settings)

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


# Ad-hoc UI-only entry point for ``uvicorn llm_proxy.app:app`` (default settings).
app = create_ui_app(Context(Settings()))


def cli_overrides(argv: list[str] | None = None) -> dict[str, str | int]:
    """Parse the command line into settings overrides.

    Usage: ``llm-proxy [UPSTREAM_BASE_URL] [--host HOST] [--llm-port N] [--ui-port N]``.
    Returns only the options actually given, keyed by ``Settings`` field name,
    so the result can be passed straight to ``Settings(**overrides)``.
    """
    parser = argparse.ArgumentParser(
        prog="llm-proxy",
        description="Transparent LLM API proxy with a live conversation WebUI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("upstream", nargs="?", metavar="UPSTREAM_BASE_URL", help="upstream base URL")
    parser.add_argument("--host", metavar="HOST", help="listen host for both listeners (default 0.0.0.0)")
    parser.add_argument("--llm-port", metavar="PORT", type=int, help="LLM proxy listen port (default 8080)")
    parser.add_argument("--ui-port", metavar="PORT", type=int, help="WebUI/API listen port (default 9090)")
    parser.add_argument("--upstream-api-key", metavar="KEY", help="server-side fallback API key")
    parser.add_argument(
        "--log-level", metavar="LEVEL", help="app log level (verbose, debug, info, warning, error)"
    )
    parser.add_argument("--ui-dir", metavar="DIR", help="static UI directory")
    args = parser.parse_args(argv)
    opts = {
        "upstream_base_url": args.upstream,
        "listen_host": args.host,
        "llm_port": args.llm_port,
        "ui_port": args.ui_port,
        "upstream_api_key": args.upstream_api_key,
        "log_level": args.log_level,
        "ui_dir": args.ui_dir,
    }
    return {key: value for key, value in opts.items() if value is not None}


# Graceful shutdown budget (seconds) per listener. Past it, uvicorn cancels
# lingering work - in-flight chat streams, persistent /models/sse feeds, the UI
# WebSocket - instead of waiting for them to end on their own, so
# ``docker compose down`` finishes well inside its 10s default grace period.
SHUTDOWN_GRACE = 2


async def _run(ctx: Context) -> None:
    """Serve both listeners in one process under a single shared signal handler.

    Two ``uvicorn.Server`` objects must not each call ``serve()``: its
    ``capture_signals`` wrapper would let the second server overwrite the first's
    signal handlers and re-raise the captured signal LIFO on shutdown. Instead we
    drive both with ``_serve()`` under one handler we install and restore.

    SIGINT/SIGTERM requests a graceful exit on both listeners; lingering work is
    cancelled after ``SHUTDOWN_GRACE`` seconds, and a second SIGINT skips even
    that (uvicorn's ``force_exit`` semantics).
    """
    settings = ctx.settings
    # log_config=None keeps uvicorn from installing its own handlers, so its
    # records propagate through the app's root handler (one timestamped format);
    # log_level="warning" silences its startup/access chatter, access_log=False
    # drops per-request access lines entirely.
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                create_llm_app(ctx),
                host=settings.listen_host,
                port=settings.llm_port,
                timeout_graceful_shutdown=SHUTDOWN_GRACE,
                log_config=None,
                log_level="warning",
                access_log=False,
            )
        ),
        uvicorn.Server(
            uvicorn.Config(
                create_ui_app(ctx),
                host=settings.listen_host,
                port=settings.ui_port,
                timeout_graceful_shutdown=SHUTDOWN_GRACE,
                log_config=None,
                log_level="warning",
                access_log=False,
            )
        ),
    ]
    originals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    seen_int = 0

    def _on_signal(signum, _frame):
        nonlocal seen_int
        if signum is signal.SIGINT and seen_int >= 1:
            # A second interrupt force-quits, matching uvicorn's own behaviour.
            for srv in servers:
                srv.force_exit = True
            return
        seen_int += 1
        for srv in servers:
            srv.should_exit = True

    async def _announce() -> None:
        # Log only once both listeners are actually bound (Server.started).
        while not all(srv.started for srv in servers):
            await asyncio.sleep(0.05)
        log.info("LLM proxy listening on http://%s:%d", settings.listen_host, settings.llm_port)
        log.info("Web UI available on http://%s:%d", settings.listen_host, settings.ui_port)

    for sig in originals:
        signal.signal(sig, _on_signal)
    announce = asyncio.create_task(_announce())
    try:
        await asyncio.gather(servers[0]._serve(), servers[1]._serve())
    finally:
        announce.cancel()
        for sig, handler in originals.items():
            signal.signal(sig, handler)


def main() -> None:
    """Console entry point (``llm-proxy``): command-line settings over defaults."""
    settings = Settings(**cli_overrides())
    asyncio.run(_run(Context(settings)))
