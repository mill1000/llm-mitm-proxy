"""WebSocket fan-out hub for the live UI.

The proxy's hot path (client request -> upstream -> client response) must never
block on the UI. So ``Hub.emit`` is a *synchronous, non-blocking* call that only
enqueues a JSON line onto each relevant connection's bounded queue. A per-
connection reader task drains the queue and writes to the socket, providing
natural backpressure: a slow or wedged UI subscriber drops its *oldest* buffered
events (freshest wins) instead of stalling token delivery to the real client.

Event model (PLAN §4): every message is ``{type, conversation_id, ...}``. A
connection has a single *focus* conversation and receives full events for it;
it receives lightweight ``activity`` pings (dock pulses) for all others.

Liveness: each connection also gets a ``{type: "ping"}`` every
``ws_ping_interval``; the UI replies ``{type: "pong"}``. If no pong arrives
within ``ws_ping_timeout`` after the last one, the socket is closed and the
connection pruned. This is what detects half-open sockets (a network drop
without FIN), which the receive loop alone can never see.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from starlette.websockets import WebSocket

log = logging.getLogger("llm_proxy.ws")


class _Conn:
    __slots__ = ("focus", "last_pong", "pinger", "queue", "reader", "ws")

    def __init__(self, ws: WebSocket, size: int):
        self.ws = ws
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=size)
        self.reader: asyncio.Task | None = None
        self.pinger: asyncio.Task | None = None
        self.focus: str | None = None
        self.last_pong = time.time()

    def enqueue(self, msg: str) -> None:
        # Drop the oldest buffered event on overflow so the freshest tokens win.
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(msg)


class Hub:
    def __init__(self, queue_size: int = 1000, ping_interval: float = 20.0, ping_timeout: float = 20.0):
        self._conns: dict[WebSocket, _Conn] = {}
        self._queue_size = queue_size
        self._send_timeout = 5.0
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout

    async def connect(self, ws: WebSocket) -> _Conn:
        await ws.accept()
        conn = _Conn(ws, self._queue_size)
        self._conns[ws] = conn
        conn.reader = asyncio.create_task(self._drain(conn))
        conn.pinger = asyncio.create_task(self._ping_loop(conn))
        log.debug(
            "ws connected from %s (%d total) [ping %.2gs/%.2gs]",
            ws.client,
            len(self._conns),
            self._ping_interval,
            self._ping_timeout,
        )
        return conn

    def pong(self, ws: WebSocket) -> None:
        conn = self._conns.get(ws)
        if conn is not None:
            conn.last_pong = time.time()

    def set_focus(self, ws: WebSocket, conversation_id: str | None) -> None:
        conn = self._conns.get(ws)
        if conn is not None:
            if conn.focus != conversation_id:
                log.debug("ws focus %s -> %s", ws.client, conversation_id or "-")
            conn.focus = conversation_id

    def disconnect(self, ws: WebSocket) -> None:
        conn = self._conns.pop(ws, None)
        if conn is not None:
            for task in (conn.reader, conn.pinger):
                if task is not None and not task.done():
                    task.cancel()
            log.debug("ws disconnected from %s (%d remain)", ws.client, len(self._conns))

    def emit(
        self, conversation_id: str, event: dict[str, Any], *, dock: bool = False, broadcast: bool = False
    ) -> None:
        """Fan out ``event`` to the connection focused on ``conversation_id``.

        When ``dock`` is set, every *other* connection gets a lightweight
        ``activity`` ping (drives the dock's "new activity" pulse). When
        ``broadcast`` is set, the *full* event goes to every connection, for
        inherently global events such as ``client_seen`` (a new client has no
        existing focused conversation to deliver to). Called from the request
        hot path, so it must not await or block.
        """
        if not self._conns:
            return
        full = json.dumps(event)
        dock_msg = (
            json.dumps({"type": "activity", "conversation_id": conversation_id, "ts": event.get("ts")})
            if dock
            else None
        )
        for conn in list(self._conns.values()):
            if broadcast or conn.focus == conversation_id:
                conn.enqueue(full)
            elif dock_msg is not None:
                conn.enqueue(dock_msg)

    async def _drain(self, conn: _Conn) -> None:
        try:
            while True:
                msg = await conn.queue.get()
                if msg is None:
                    break
                await asyncio.wait_for(conn.ws.send_text(msg), timeout=self._send_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead socket must never take down the loop
            log.warning("ws drain ended for %s: %s: %s", conn.ws.client, type(exc).__name__, exc)
            if conn.pinger is not None and not conn.pinger.done():
                conn.pinger.cancel()
        finally:
            # Remove on our own; do not cancel() the running task from itself.
            self._conns.pop(conn.ws, None)

    async def _ping_loop(self, conn: _Conn) -> None:
        """Send liveness pings and prune the connection if it stops pinging back.

        A client that never pongs (or is half-open) is closed after
        ``ping_interval + ping_timeout`` of silence. The close makes the
        endpoint's receive loop raise, which runs ``hub.disconnect``.
        """
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                if conn.ws not in self._conns:
                    return  # already disconnected
                silent = time.time() - conn.last_pong
                if silent > self._ping_interval + self._ping_timeout:
                    log.warning(
                        "ws liveness: %s silent for >%.2gs, closing (code=1011)", conn.ws.client, silent
                    )
                    await conn.ws.close(code=1011)
                    return
                await conn.ws.send_text(json.dumps({"type": "ping", "ts": time.time()}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead socket must never take down the loop
            log.debug("ws pinger ended for %s: %s: %s", conn.ws.client, type(exc).__name__, exc)
