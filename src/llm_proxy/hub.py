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
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from starlette.websockets import WebSocket

log = logging.getLogger("llm_proxy.ws")


class _Conn:
    __slots__ = ("focus", "queue", "reader", "ws")

    def __init__(self, ws: WebSocket, size: int):
        self.ws = ws
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=size)
        self.reader: asyncio.Task | None = None
        self.focus: str | None = None

    def enqueue(self, msg: str) -> None:
        # Drop the oldest buffered event on overflow so the freshest tokens win.
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(msg)


class Hub:
    def __init__(self, queue_size: int = 1000):
        self._conns: dict[WebSocket, _Conn] = {}
        self._queue_size = queue_size
        self._send_timeout = 5.0

    async def connect(self, ws: WebSocket) -> _Conn:
        await ws.accept()
        conn = _Conn(ws, self._queue_size)
        self._conns[ws] = conn
        conn.reader = asyncio.create_task(self._drain(conn))
        log.info("ws connected from %s (%d total)", ws.client, len(self._conns))
        return conn

    def set_focus(self, ws: WebSocket, conversation_id: str | None) -> None:
        conn = self._conns.get(ws)
        if conn is not None:
            if conn.focus != conversation_id:
                log.info("ws focus %s -> %s", ws.client, conversation_id or "-")
            conn.focus = conversation_id

    def disconnect(self, ws: WebSocket) -> None:
        conn = self._conns.pop(ws, None)
        if conn is not None:
            if conn.reader is not None and not conn.reader.done():
                conn.reader.cancel()
            log.info("ws disconnected from %s (%d remain)", ws.client, len(self._conns))

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
            # A dropped *broadcast* (e.g. client_seen) means the UI was blind to a
            # new client - log it loudly, it is the classic "empty dock" diagnosis.
            log.log(
                logging.WARNING if broadcast else logging.DEBUG,
                "emit %s dropped: no ws connections",
                event.get("type"),
            )
            return
        full = json.dumps(event)
        dock_msg = (
            json.dumps({"type": "activity", "conversation_id": conversation_id, "ts": event.get("ts")})
            if dock
            else None
        )
        n_full = n_dock = 0
        for conn in list(self._conns.values()):
            if broadcast or conn.focus == conversation_id:
                conn.enqueue(full)
                n_full += 1
            elif dock_msg is not None:
                conn.enqueue(dock_msg)
                n_dock += 1
        # Per-chunk (non-broadcast) emits are hot-path and stay DEBUG; broadcasts
        # are rare and are the load-bearing events for the dock, so they are INFO.
        log.log(
            logging.INFO if broadcast else logging.DEBUG,
            "emit %s -> %d full, %d dock",
            event.get("type"),
            n_full,
            n_dock,
        )

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
        finally:
            # Remove on our own; do not cancel() the running task from itself.
            self._conns.pop(conn.ws, None)
