"""In-flight exchange visibility: REST shows a pending exchange while the upstream
call is still running, and finalizes it in place (same sequence) on completion.

This is what keeps a conversation from appearing empty when a client's first
request is long-running: the exchange_started WS event is focus-scoped and is
lost to a UI that only now focuses the conversation, so REST must carry the
pending state.
"""

from __future__ import annotations

import threading
import time
import unittest

from fastapi.testclient import TestClient

from llm_proxy.app import create_app
from llm_proxy.config import Settings

try:  # package form (unittest discover)
    from .mock_upstream import start_mock, stop_mock
except ImportError:  # direct execution fallback
    from mock_upstream import start_mock, stop_mock  # type: ignore

BASE = Settings(upstream_base_url="http://127.0.0.1:8082", log_level="critical")

MOCK_PORT = 8082
CHAT = "/v1/chat/completions"
CID = "testclient"  # the source host Starlette's TestClient presents to the proxy


class TestInFlight(unittest.TestCase):
    """Each test uses a fresh proxy app (isolated in-memory store) against the shared mock."""

    @classmethod
    def setUpClass(cls):
        start_mock(MOCK_PORT)

    @classmethod
    def tearDownClass(cls):
        try:
            stop_mock()
        except Exception:  # noqa: BLE001
            pass

    def setUp(self):
        # httpx2 sends a default User-Agent, which the proxy folds into the
        # client/conversation id; suppress it so the id stays "testclient".
        self.client = TestClient(create_app(BASE), headers={"User-Agent": ""})
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def _slow_post(self, stream: bool) -> threading.Thread:
        t = threading.Thread(
            target=lambda: self.client.post(
                CHAT,
                json={"model": "slow", "stream": stream, "messages": [{"role": "user", "content": "hi"}]},
            ),
            daemon=True,
        )
        t.start()
        return t

    def _exchanges(self) -> list[dict]:
        return self.client.get(f"/api/conversations/{CID}").json()["exchanges"]

    def test_in_flight_visible_in_rest_then_finalized(self):
        """A slow non-stream request is visible as in_flight in REST, and completes
        in place: same id/sequence, response filled in, in_flight cleared."""
        t = self._slow_post(stream=False)
        time.sleep(0.3)  # inside the mock's 0.8s hold
        exs = self._exchanges()
        self.assertEqual(len(exs), 1)
        pending = exs[0]
        self.assertTrue(pending["in_flight"])
        self.assertIsNone(pending["server_response"])
        self.assertFalse(pending["streaming"])
        self.assertEqual(pending["client_request"]["method"], "POST")
        self.assertEqual(pending["client_request"]["body_json"]["model"], "slow")
        seq = pending["sequence"]

        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        exs = self._exchanges()
        self.assertEqual(len(exs), 1)
        done = exs[0]
        self.assertFalse(done["in_flight"])
        self.assertEqual(done["id"], pending["id"])
        self.assertEqual(done["sequence"], seq)
        self.assertEqual(done["server_response"]["status"], 200)

    def test_in_flight_carries_streaming_flag(self):
        """A slow streaming request is visible as in_flight with streaming set, so
        the UI can render the pending card as a stream."""
        t = self._slow_post(stream=True)
        time.sleep(0.3)
        exs = self._exchanges()
        self.assertEqual(len(exs), 1)
        self.assertTrue(exs[0]["in_flight"])
        self.assertTrue(exs[0]["streaming"])
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
