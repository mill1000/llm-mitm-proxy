"""M3 tests: WS liveness (ping/pong + dead-socket pruning), upstream timeout policy."""

from __future__ import annotations

import json
import time
import unittest

from fastapi.testclient import TestClient

from llm_proxy.app import Context, create_ui_app
from llm_proxy.config import Settings

# Settings for the app under test: the local mock upstream (no M3 test makes an
# upstream call, but the app factory reads the value at startup), quiet logs.
BASE = Settings(upstream_base_url="http://127.0.0.1:8082", log_level="critical")


def _fresh_client(**overrides) -> TestClient:
    """An entered UI TestClient with a fresh Context; ``overrides`` are Settings fields."""
    ctx = Context(BASE.model_copy(update=overrides))
    client = TestClient(create_ui_app(ctx))
    client.__enter__()
    return client


def _close_client(client: TestClient) -> None:
    client.__exit__(None, None, None)


class TestUpstreamTimeouts(unittest.TestCase):
    def test_upstream_timeout_policy_reaches_the_client(self):
        """connect/pool settings reach the pooled client, and read is intentionally
        unbounded: a gap cap would kill silent persistent streams (/models/sse)
        and long prefill/think phases, so timeout policy belongs to the client."""
        client = _fresh_client(
            upstream_connect_timeout=1.5,
            upstream_pool_timeout=2.5,
        )
        try:
            t = client.app.state.http.timeout
            self.assertEqual(t.connect, 1.5)
            self.assertIsNone(t.read)
            self.assertEqual(t.pool, 2.5)
        finally:
            _close_client(client)


class TestWsLiveness(unittest.TestCase):
    """Liveness with fast pings (0.3s interval/timeout): a ponging socket keeps
    receiving pings, a silent one is closed with 1011 and pruned from the hub.

    With interval=timeout=0.3 the pinger checks at ~0.3s, ~0.6s, ~0.9s: a
    silent socket is closed at the first check where silence > 0.6s (~0.9s),
    and a ponging socket survives because its last_pong stays fresh.
    """

    def setUp(self):
        self.client = _fresh_client(ws_ping_interval=0.3, ws_ping_timeout=0.3)

    def tearDown(self):
        _close_client(self.client)

    def _receive_ping(self, ws) -> dict:
        """Block for the next frame and assert it is a liveness ping, not a close.

        (Raw ASGI app->client text frames are typed ``websocket.send``.)
        """
        msg = ws.receive()
        self.assertEqual(msg["type"], "websocket.send")
        ev = json.loads(msg["text"])
        self.assertEqual(ev["type"], "ping")
        self.assertIn("ts", ev)
        return ev

    def test_ws_ping_pong_keeps_socket_alive(self):
        hub = self.client.app.state.hub
        with self.client.websocket_connect("/ws") as ws:
            ping1 = self._receive_ping(ws)
            ws.send_json({"type": "pong", "ts": ping1["ts"]})
            ping2 = self._receive_ping(ws)
            ws.send_json({"type": "pong", "ts": ping2["ts"]})
            # A socket that never pongs is closed at this check; a ponging one
            # still receives the next ping.
            self._receive_ping(ws)
            self.assertEqual(len(hub._conns), 1)

    def test_ws_silent_socket_pruned(self):
        hub = self.client.app.state.hub
        with self.client.websocket_connect("/ws") as ws:
            self.assertEqual(len(hub._conns), 1)
            # No pong at all: the pinger sends pings until silence exceeds
            # interval + timeout, then must close the socket with 1011.
            # Bounded: the close arrives within ~3 intervals.
            msg = ws.receive()
            while msg["type"] == "websocket.send":  # skip liveness pings
                msg = ws.receive()
            self.assertEqual(msg["type"], "websocket.close")
            self.assertEqual(msg.get("code"), 1011)
        time.sleep(0.1)  # the endpoint's finally() runs hub.disconnect after the close
        self.assertEqual(len(hub._conns), 0)


if __name__ == "__main__":
    unittest.main()
