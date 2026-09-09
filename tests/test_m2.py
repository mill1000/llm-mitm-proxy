"""M2 tests: replay (re-send a captured client request upstream) and single-exchange export."""

from __future__ import annotations

import unittest

try:  # package form (unittest discover)
    from . import mock_upstream
    from .helpers import clients, close
    from .mock_upstream import start_mock, stop_mock
except ImportError:  # direct execution fallback
    import mock_upstream  # type: ignore
    from helpers import clients, close  # type: ignore
    from mock_upstream import start_mock, stop_mock  # type: ignore

from llm_proxy.config import Settings

# Settings for the app under test: the local mock upstream, quiet logs.
BASE = Settings(upstream_base_url="http://127.0.0.1:8082", log_level="critical")

MOCK_PORT = 8082
CHAT = "/v1/chat/completions"
CID = "testclient"  # the source host Starlette's TestClient presents to the proxy


def _payload(stream: bool) -> dict:
    return {"model": "local-model", "stream": stream, "messages": [{"role": "user", "content": "hi"}]}


def _replay_path(cid: str, seq: int) -> str:
    return f"/api/conversations/{cid}/exchanges/{seq}/replay"


class TestReplayAndExport(unittest.TestCase):
    """Each test uses a fresh shared Context (isolated store) against the shared mock."""

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

    def test_replay_as_is(self):
        self.llm.post(CHAT, json=_payload(stream=False))
        original = mock_upstream.REQUESTS[-1]

        r = self.ui.post(_replay_path(CID, 0), json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "Hello, world!")

        # The upstream received the captured body again, verbatim.
        self.assertEqual(mock_upstream.REQUESTS[-1], original)

        conv = self.ui.get(f"/api/conversations/{CID}").json()
        self.assertEqual(conv["exchange_count"], 2)
        replayed = conv["exchanges"][-1]
        self.assertTrue(replayed["is_replay"])
        self.assertEqual(replayed["sequence"], 1)
        self.assertEqual(replayed["client_request"]["body_json"], original)

    def test_replay_replaces_body(self):
        self.llm.post(CHAT, json=_payload(stream=False))

        edited = {**_payload(stream=False), "model": "thinker", "temperature": 0.2}
        r = self.ui.post(_replay_path(CID, 0), json=edited)
        self.assertEqual(r.status_code, 200)

        # The upstream received exactly the edited body: the model and the new
        # parameter are present, nothing was merged over the captured request.
        self.assertEqual(mock_upstream.REQUESTS[-1], edited)

        conv = self.ui.get(f"/api/conversations/{CID}").json()
        replayed = conv["exchanges"][-1]
        self.assertTrue(replayed["is_replay"])
        self.assertEqual(replayed["client_request"]["body_json"]["model"], "thinker")

        # The replayed exchange is exportable and carries the replay flag.
        exp = self.ui.get(f"/api/conversations/{CID}/exchanges/{replayed['sequence']}/export").json()
        self.assertEqual(exp["format"], "llm-proxy/exchange")
        self.assertTrue(exp["exchange"]["is_replay"])

    def test_replay_streaming(self):
        self.llm.post(CHAT, json=_payload(stream=True))

        r = self.ui.post(_replay_path(CID, 0), json={})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers.get("content-type", ""))
        self.assertIn("[DONE]", r.text)
        self.assertIn('"Hello"', r.text)  # the answer streams in pieces

        conv = self.ui.get(f"/api/conversations/{CID}").json()
        replayed = conv["exchanges"][-1]
        self.assertTrue(replayed["is_replay"])
        self.assertTrue(replayed["server_response"]["streaming"])
        self.assertEqual(
            replayed["server_response"]["reassembled"]["choices"][0]["message"]["content"], "Hello, world!"
        )

    def test_replay_unknown_seq_and_conversation_404(self):
        self.llm.post(CHAT, json=_payload(stream=False))
        self.assertEqual(self.ui.post(_replay_path(CID, 99), json={}).status_code, 404)
        self.assertEqual(self.ui.post(_replay_path("nope", 0), json={}).status_code, 404)
        self.assertEqual(self.ui.get("/api/conversations/nope/exchanges/0/export").status_code, 404)
        self.assertEqual(self.ui.get(f"/api/conversations/{CID}/exchanges/99/export").status_code, 404)

    def test_single_exchange_export(self):
        self.llm.post(CHAT, json=_payload(stream=False))

        r = self.ui.get(f"/api/conversations/{CID}/exchanges/0/export")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("content-disposition"), f'attachment; filename="{CID}-ex0.json"')
        exp = r.json()
        self.assertEqual(exp["format"], "llm-proxy/exchange")
        self.assertEqual(exp["version"], 1)
        self.assertEqual(exp["exchange"]["sequence"], 0)
        self.assertFalse(exp["exchange"]["is_replay"])
        self.assertEqual(
            exp["exchange"]["server_response"]["body_json"]["choices"][0]["message"]["content"],
            "Hello, world!",
        )

    def test_conversation_export_redacts_secrets(self):
        self.llm.post(
            CHAT, json=_payload(stream=False), headers={"Authorization": "Bearer sk-export-secret-123"}
        )
        clients_body = self.ui.get("/api/clients").json()
        cid = clients_body[0]["conversation_ids"][0]

        r = self.ui.get(f"/api/conversations/{cid}/export")
        self.assertEqual(r.status_code, 200)
        exp = r.json()
        self.assertEqual(exp["format"], "llm-proxy/conversation")
        self.assertEqual(exp["version"], 1)
        self.assertIn("stats", exp)
        self.assertGreaterEqual(len(exp["exchanges"]), 1)
        # The authorization header is masked in the dump (the client id may still
        # contain the key by design, since the key is part of client identity).
        auth = exp["exchanges"][0]["client_request"]["headers"]["authorization"]
        self.assertIn("***", auth)
        self.assertNotIn("sk-export-secret-123", auth)


if __name__ == "__main__":
    unittest.main()
