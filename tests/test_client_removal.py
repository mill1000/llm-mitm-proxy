"""Client removal: DELETE /api/clients/{id} drops a client and all of its
conversations; a new request re-registers the client from scratch."""

from __future__ import annotations

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


class TestClientRemoval(unittest.TestCase):
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

    def _register(self):
        r = self.llm.post(
            CHAT,
            json={"model": "local-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(r.status_code, 200)

    def _client_ids(self) -> list[str]:
        return [c["id"] for c in self.ui.get("/api/clients").json()]

    def test_remove_client(self):
        self._register()
        clients = self.ui.get("/api/clients").json()
        c = next(x for x in clients if x["id"] == CID)
        cids = c["conversation_ids"]
        self.assertEqual(len(cids), 1)

        r = self.ui.delete(f"/api/clients/{CID}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})

        self.assertNotIn(CID, self._client_ids())
        self.assertEqual(self.ui.get(f"/api/conversations/{cids[0]}").status_code, 404)
        self.assertEqual(self.ui.delete(f"/api/clients/{CID}").status_code, 404)

    def test_removed_client_reregisters(self):
        self._register()
        self.assertEqual(self.ui.delete(f"/api/clients/{CID}").status_code, 200)
        self._register()
        self.assertIn(CID, self._client_ids())
        conv = self.ui.get(f"/api/conversations/{CID}").json()
        self.assertEqual(len(conv["exchanges"]), 1)
