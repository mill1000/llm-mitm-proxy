"""M3 tests: adapter fail-fast, WS liveness (ping/pong + dead-socket pruning), upstream timeouts."""

from __future__ import annotations

import json
import os
import time
import unittest

# Point the proxy at the local mock BEFORE settings are read (no M3 test makes an
# upstream call, but the app factory reads the value at startup).
os.environ["UPSTREAM_BASE_URL"] = "http://127.0.0.1:8082"
os.environ["LISTEN_PORT"] = "9090"
os.environ["LOG_LEVEL"] = (
    "critical"  # keep the suite quiet; tests that assert on logs capture their own handler
)

from fastapi.testclient import TestClient  # noqa: E402

from llm_proxy.app import create_app  # noqa: E402
from llm_proxy.config import get_settings  # noqa: E402


def _set_env(pairs: dict[str, str]) -> dict[str, str | None]:
    """Apply env overrides; return the previous values for restoration."""
    saved = {k: os.environ.pop(k, None) for k in pairs}
    os.environ.update(pairs)
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    """Undo _set_env: re-set previous values, or remove keys that did not exist."""
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _fresh_client() -> TestClient:
    """A TestClient with a fresh app (entered). get_settings() is lru_cached, so
    the env must already reflect the settings under test before this is called."""
    get_settings.cache_clear()
    client = TestClient(create_app())
    client.__enter__()
    return client


def _close_client(client: TestClient) -> None:
    client.__exit__(None, None, None)
    get_settings.cache_clear()


class TestAdapterFailFast(unittest.TestCase):
    def test_unknown_in_adapter_fails_startup(self):
        """A bogus IN_ADAPTER must fail loudly at startup and list the available
        adapters - not on the first proxied request."""
        saved = _set_env({"IN_ADAPTER": "does-not-exist"})
        try:
            with self.assertRaises(KeyError) as ctx:
                _fresh_client()
            self.assertIn("does-not-exist", str(ctx.exception))
            self.assertIn("openai", str(ctx.exception))
        finally:
            _restore_env(saved)
            get_settings.cache_clear()


class TestUpstreamTimeouts(unittest.TestCase):
    def test_upstream_timeouts_reach_the_client(self):
        """UPSTREAM_*_TIMEOUT must reach the pooled httpx2 client as connect/read/pool."""
        saved = _set_env(
            {
                "UPSTREAM_CONNECT_TIMEOUT": "1.5",
                "UPSTREAM_READ_TIMEOUT": "7.5",
                "UPSTREAM_POOL_TIMEOUT": "2.5",
            }
        )
        try:
            client = _fresh_client()
            try:
                t = client.app.state.http.timeout
                self.assertEqual(t.connect, 1.5)
                self.assertEqual(t.read, 7.5)
                self.assertEqual(t.pool, 2.5)
            finally:
                _close_client(client)
        finally:
            _restore_env(saved)
            get_settings.cache_clear()


class TestWsLiveness(unittest.TestCase):
    """Liveness with fast pings (0.3s interval/timeout): a ponging socket keeps
    receiving pings, a silent one is closed with 1011 and pruned from the hub.

    With interval=timeout=0.3 the pinger checks at ~0.3s, ~0.6s, ~0.9s: a
    silent socket is closed at the first check where silence > 0.6s (~0.9s),
    and a ponging socket survives because its last_pong stays fresh.
    """

    def setUp(self):
        self._saved = _set_env({"WS_PING_INTERVAL": "0.3", "WS_PING_TIMEOUT": "0.3"})
        self.client = _fresh_client()

    def tearDown(self):
        _close_client(self.client)
        _restore_env(self._saved)

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
