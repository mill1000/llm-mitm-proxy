"""The UI listener's /api/* surface: replay (re-send a captured client request
upstream), single-exchange and conversation export (secrets redacted), client
and exchange removal, and static UI serving. Each test uses a fresh shared
Context (isolated in-memory store) against the shared mock upstream."""

from __future__ import annotations

import threading
import time
import unittest

try:  # package form (unittest discover)
    from . import mock_upstream
    from .helpers import CHAT, CID, MockedCase, payload
except ImportError:  # direct execution fallback
    import mock_upstream  # type: ignore
    from helpers import CHAT, CID, MockedCase, payload  # type: ignore


def _replay_path(cid: str, seq: int) -> str:
    return f"/api/conversations/{cid}/exchanges/{seq}/replay"


class TestReplayAndExport(MockedCase):
    def test_replay_as_is(self):
        self.llm.post(CHAT, json=payload(stream=False))
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
        self.llm.post(CHAT, json=payload(stream=False))

        edited = {**payload(stream=False), "model": "thinker", "temperature": 0.2}
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
        self.llm.post(CHAT, json=payload(stream=True))

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
        self.llm.post(CHAT, json=payload(stream=False))
        self.assertEqual(self.ui.post(_replay_path(CID, 99), json={}).status_code, 404)
        self.assertEqual(self.ui.post(_replay_path("nope", 0), json={}).status_code, 404)
        self.assertEqual(self.ui.get("/api/conversations/nope/exchanges/0/export").status_code, 404)
        self.assertEqual(self.ui.get(f"/api/conversations/{CID}/exchanges/99/export").status_code, 404)

    def test_single_exchange_export(self):
        self.llm.post(CHAT, json=payload(stream=False))

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
            CHAT, json=payload(stream=False), headers={"Authorization": "Bearer sk-export-secret-123"}
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


class TestClientRemoval(MockedCase):
    """DELETE /api/clients/{id} drops a client and all of its conversations; a new
    request re-registers the client from scratch."""

    def _register(self):
        r = self.llm.post(CHAT, json=payload(stream=False))
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


class TestExchangeRemoval(MockedCase):
    """Exchanges are removed by sequence. In-flight ones may be removed too: the
    upstream call continues but its finalization is dropped, so the exchange is
    never re-appended. Removal leaves the remaining exchanges' sequences intact."""

    def _chat(self, model: str = "local-model") -> None:
        self.llm.post(CHAT, json=payload(stream=False, model=model)).raise_for_status()

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


class TestUiServing(MockedCase):
    """The WebUI SPA and its assets are served from the UI listener's static
    mount (no placeholder links, no browser caching, favicon.ico -> SVG)."""

    def test_serves_ui_and_favicon(self):
        # The status page is served from the ui/ dir and must not contain
        # placeholder conversation links (literal <id> hrefs -> junk 404s).
        r = self.ui.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/favicon.svg"', r.text)
        self.assertNotIn('href="/api/conversations', r.text)

        # Browsers probe /favicon.ico even with an SVG icon link; serve the SVG.
        r = self.ui.get("/favicon.ico")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/svg+xml")
        self.assertIn("<svg", r.text)

    def test_ui_assets_served(self):
        # The SPA is served from the UI listener's static mount.
        r = self.ui.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('src="/app.js"', r.text)
        self.assertIn('href="/styles.css"', r.text)
        for asset in ("/app.js", "/styles.css"):
            r = self.ui.get(asset)
            self.assertEqual(r.status_code, 200, asset)
            self.assertTrue(len(r.content) > 0, asset)
            # UI assets must not be heuristically cached by browsers, or a proxy
            # restart can leave a tab running stale JS against the new process.
            self.assertEqual(r.headers.get("cache-control"), "no-cache", asset)


if __name__ == "__main__":
    unittest.main()
