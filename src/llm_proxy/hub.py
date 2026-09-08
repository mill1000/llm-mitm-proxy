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
from typing import Any

from starlette.websockets import WebSocket


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
        return conn

    def set_focus(self, ws: WebSocket, conversation_id: str | None) -> None:
        conn = self._conns.get(ws)
        if conn is not None:
            conn.focus = conversation_id

    def disconnect(self, ws: WebSocket) -> None:
        conn = self._conns.pop(ws, None)
        if conn is not None and conn.reader is not None and not conn.reader.done():
            conn.reader.cancel()

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
        except Exception:  # noqa: BLE001 - a dead socket must never take down the loop
            pass
        finally:
            # Remove on our own; do not cancel() the running task from itself.
            self._conns.pop(conn.ws, None)
