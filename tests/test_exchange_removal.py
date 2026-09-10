"""Removing individual exchanges from a conversation via the UI API.

Exchanges are removed by sequence. In-flight ones may be removed too: the
upstream call continues but its finalization is dropped, so the exchange is
never re-appended. Removal leaves the remaining exchanges' sequences intact.
"""

from __future__ import annotations

import threading
import time
import unittest

try:  # package form (unittest discover)
    from .helpers import clients, close
    from .mock_upstream import start_mock, stop_mock
except ImportError:  # direct execution fallback
    from helpers import clients, close  # type: ignore
    from mock_upstream import start_mock, stop_mock  # type: ignore

from llm_proxy.config import Settings

BASE = Settings(upstream_base_url="http://127.0.0.1:8082", log_level="critical")

MOCK_PORT = 8082
CHAT = "/v1/chat/completions"
CID = "testclient"  # the source host Starlette's TestClient presents to the proxy


class TestExchangeRemoval(unittest.TestCase):
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
        self.ctx, self.llm, self.ui = clients(BASE)

    def tearDown(self):
        close(self.llm, self.ui)

    def _chat(self, model: str = "local-model") -> None:
        self.llm.post(
            CHAT, json={"model": model, "stream": False, "messages": [{"role": "user", "content": "hi"}]}
        ).raise_for_status()

    def _exchanges(self) -> list[dict]:
        return self.ui.get(f"/api/conversations/{CID}").json()["exchanges"]

    def _remove(self, seq: int):
        return self.ui.request("DELETE", f"/api/conversations/{CID}/exchanges/{seq}")

    def test_remove_completed_exchange(self):
        self._chat()
        self._chat()
        self.assertEqual(len(self._exchanges()), 2)
        self.assertEqual(self._remove(0).status_code, 200)
        exs = self._exchanges()
        self.assertEqual(len(exs), 1)
        self.assertEqual(exs[0]["sequence"], 1)  # sequences are not renumbered

    def test_remove_missing_exchange(self):
        self._chat()
        self.assertEqual(self._remove(99).status_code, 404)
        self.assertEqual(self._remove(0).status_code, 200)
        self.assertEqual(self._remove(0).status_code, 404)  # already gone

    def test_remove_unknown_conversation(self):
        self.assertEqual(self.ui.request("DELETE", "/api/conversations/nope/exchanges/0").status_code, 404)

    def test_in_flight_exchange_removed_is_not_readded(self):
        t = threading.Thread(target=lambda: self._chat(model="slow"), daemon=True)
        t.start()
        time.sleep(0.3)  # inside the mock's 0.8s hold
        self.assertEqual(self._remove(0).status_code, 200)
        self.assertEqual(len(self._exchanges()), 0)
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(self._exchanges()), 0)
