"""The transparent LLM proxy: verbatim passthrough (query strings, upstream
errors, non-JSON bodies, undecoded SSE), streaming tap-and-forward, upstream
timeout policy, in-flight exchange visibility, client identity, and the
two-port split. Each test uses a fresh shared Context (isolated store) against
the shared mock upstream."""

from __future__ import annotations

import socket
import threading
import time
import unittest
from urllib.parse import quote

from fastapi.testclient import TestClient

try:  # package form (unittest discover)
    from .helpers import BASE, CHAT, CID, MockedCase, close_ui, payload, ui_client
except ImportError:  # direct execution fallback
    from helpers import BASE, CHAT, CID, MockedCase, close_ui, payload, ui_client  # type: ignore

from llm_proxy.app import Context, create_llm_app, create_ui_app


class TestProxy(MockedCase):
    def test_health(self):
        r = self.ui.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["upstream"], "ok")
        self.assertEqual(body["upstream_url"], BASE.upstream_base_url)

    def test_health_reports_upstream_error_when_unreachable(self):
        # /health must report the *link* state, not just the proxy process: a
        # dead upstream shows up as upstream=error (the UI pill turns red).
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()

        dead = BASE.model_copy(update={"upstream_base_url": f"http://127.0.0.1:{dead_port}"})
        with TestClient(create_ui_app(Context(dead))) as c:
            body = c.get("/health").json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["upstream"], "error")

    def test_non_streaming_passthrough(self):
        r = self.llm.post(CHAT, json=payload(stream=False))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello, world!")
        self.assertEqual(body["usage"]["total_tokens"], 8)

    def test_streaming_tap_and_forward(self):
        r = self.llm.post(CHAT, json=payload(stream=True))
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers.get("content-type", ""))
        self.assertIn("[DONE]", r.text)
        self.assertIn("Hello", r.text)

    def test_streaming_upstream_error_status_passthrough(self):
        # Upstream returns a JSON error (not SSE) for a stream request; the proxy
        # must surface the upstream's status, not a premature 200.
        r = self.llm.post(CHAT, json=payload(stream=True, model="boom"))
        self.assertEqual(r.status_code, 404)
        self.assertIn("model not found", r.json()["error"]["message"])

    def test_streaming_upstream_unreachable_returns_502(self):
        # Upstream unreachable (connection refused) -> a clean 502, not a phantom 200.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()

        dead = BASE.model_copy(update={"upstream_base_url": f"http://127.0.0.1:{dead_port}"})
        with TestClient(create_llm_app(Context(dead))) as c:
            r = c.post(CHAT, json=payload(stream=True))
            self.assertEqual(r.status_code, 502)
            self.assertIn("upstream error", r.json()["error"]["message"])

    def test_capture_and_export(self):
        self.llm.post(CHAT, json=payload(stream=False))
        self.llm.post(CHAT, json=payload(stream=True))

        clients_body = self.ui.get("/api/clients").json()
        self.assertTrue(clients_body)
        cid = clients_body[0]["conversation_ids"][0]

        conv = self.ui.get(f"/api/conversations/{cid}").json()
        self.assertGreaterEqual(conv["exchange_count"], 2)

        stream_ex = [ex for ex in conv["exchanges"] if ex["server_response"]["streaming"]]
        self.assertTrue(stream_ex)
        content = stream_ex[0]["server_response"]["reassembled"]["choices"][0]["message"]["content"]
        self.assertEqual(content, "Hello, world!")
        self.assertEqual(stream_ex[0]["usage"]["total_tokens"], 8)

        exp = self.ui.get(f"/api/conversations/{cid}/export").json()
        self.assertEqual(exp["format"], "llm-mitm-proxy/conversation")
        self.assertEqual(exp["version"], 1)
        self.assertGreaterEqual(len(exp["exchanges"]), 2)
        self.assertGreaterEqual(exp["stats"]["total_completion_tokens"], 3)

    def test_secret_redaction(self):
        self.llm.post(
            CHAT, json=payload(stream=False), headers={"Authorization": "Bearer sk-secret-test-key-123"}
        )
        clients_body = self.ui.get("/api/clients").json()
        cid = clients_body[0]["conversation_ids"][0]
        conv = self.ui.get(f"/api/conversations/{cid}").json()
        auth = conv["exchanges"][0]["client_request"]["headers"].get("authorization")
        self.assertIsNotNone(auth)
        self.assertIn("***", auth)
        self.assertNotIn("sk-secret-test-key-123", auth)

    def test_user_agent_splits_clients_on_shared_ip(self):
        # Same source IP, different user agents -> separate conversations;
        # repeated agent -> same conversation. The UA's "/" is sanitized out of
        # the id (ids are REST path segments). The TestClient's host is
        # "testclient", so clients are matched by id suffix, not full value.
        for ua in ("curl/8.5.0", "zed/1.0.0", "curl/8.5.0"):
            self.llm.post(CHAT, json=payload(stream=False), headers={"User-Agent": ua})

        clients_body = self.ui.get("/api/clients").json()
        by_ua = {c["id"].rsplit("::", 1)[-1]: c for c in clients_body}
        self.assertEqual(set(by_ua), {"curl_8.5.0", "zed_1.0.0"})

        conv_curl = self.ui.get(
            "/api/conversations/" + quote(by_ua["curl_8.5.0"]["conversation_ids"][0])
        ).json()
        self.assertEqual(conv_curl["exchange_count"], 2)
        conv_zed = self.ui.get(
            "/api/conversations/" + quote(by_ua["zed_1.0.0"]["conversation_ids"][0])
        ).json()
        self.assertEqual(conv_zed["exchange_count"], 1)

    def test_user_agent_and_key_combined_id(self):
        self.llm.post(
            CHAT,
            json=payload(stream=False),
            headers={"User-Agent": "curl/8.5.0", "Authorization": "Bearer sk-test"},
        )
        clients_body = self.ui.get("/api/clients").json()
        self.assertEqual(len(clients_body), 1)
        self.assertTrue(clients_body[0]["id"].endswith("::curl_8.5.0::sk-test"))


class TestTransparent(MockedCase):
    """The LLM listener forwards every method/path verbatim and taps the response
    opaquely; the UI listener serves no proxy routes at all."""

    def _exchanges(self) -> list[dict]:
        """The (single) testclient's exchanges, read back through the UI API."""
        body = self.ui.get("/api/clients").json()
        c = next(x for x in body if x["id"] == CID)
        conv = self.ui.get(f"/api/conversations/{c['conversation_ids'][0]}").json()
        return conv["exchanges"]

    def test_query_string_forwarded_verbatim(self):
        # The proxy must not drop the query string: /props?model=... is how
        # llama.cpp clients probe the upstream (the Zed case that motivated the
        # two-port split).
        r = self.llm.get("/props", params={"model": "m"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"n_ctx": 4096, "model": "m"})

    def test_upstream_404_passthrough(self):
        # The proxy never 404s a client request itself; the upstream's own 404
        # is what the client sees.
        r = self.llm.get("/nope")
        self.assertEqual(r.status_code, 404)

    def test_opaque_capture_with_query(self):
        self.llm.get("/props", params={"model": "m"})
        (ex,) = self._exchanges()
        cr = ex["client_request"]
        self.assertEqual(cr["method"], "GET")
        self.assertEqual(cr["path"], "/props?model=m")
        sr = ex["server_response"]
        self.assertEqual(sr["status"], 200)
        self.assertEqual(sr["body_json"], {"n_ctx": 4096, "model": "m"})
        self.assertFalse(sr["streaming"])

    def test_non_json_response_captured_as_text(self):
        r = self.llm.get("/plain")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, "hello")
        (ex,) = self._exchanges()
        sr = ex["server_response"]
        self.assertEqual(sr["body_text"], "hello")
        self.assertIsNone(sr["body_json"])

    def test_undecoded_sse_captured_raw(self):
        r = self.llm.get("/sse")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers.get("content-type", ""))
        self.assertIn("tick", r.text)
        self.assertIn("[DONE]", r.text)
        (ex,) = self._exchanges()
        sr = ex["server_response"]
        self.assertTrue(sr["streaming"])
        self.assertIn("tick", sr["body_text"])
        # Not a decoded OpenAI stream: there is no reassembled chat body.
        self.assertNotIn("reassembled", sr)

    def test_ports_do_not_overlap(self):
        # The UI port serves no proxy routes...
        self.assertEqual(self.ui.get("/v1/models").status_code, 404)
        # ...and the LLM port serves no UI routes (both are forwarded to the
        # upstream, which does not have them).
        self.assertEqual(self.llm.get("/api/clients").status_code, 404)
        self.assertEqual(self.llm.get("/").status_code, 404)


class TestInFlight(MockedCase):
    """A slow upstream call is visible as an in-flight exchange in REST and is
    finalized in place (same id/sequence) on completion. This is what keeps a
    conversation from appearing empty when a client's first request is
    long-running."""

    def _slow_post(self, stream: bool) -> threading.Thread:
        t = threading.Thread(
            target=lambda: self.llm.post(CHAT, json=payload(stream=stream, model="slow")),
            daemon=True,
        )
        t.start()
        return t

    def _exchanges(self) -> list[dict]:
        return self.ui.get(f"/api/conversations/{CID}").json()["exchanges"]

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


class TestUpstreamTimeouts(unittest.TestCase):
    def test_upstream_timeout_policy_reaches_the_client(self):
        """connect/pool settings reach the pooled client, and read is intentionally
        unbounded: a gap cap would kill silent persistent streams (/models/sse)
        and long prefill/think phases, so timeout policy belongs to the client."""
        client = ui_client(upstream_connect_timeout=1.5, upstream_pool_timeout=2.5)
        try:
            t = client.app.state.http.timeout
            self.assertEqual(t.connect, 1.5)
            self.assertIsNone(t.read)
            self.assertEqual(t.pool, 2.5)
        finally:
            close_ui(client)


if __name__ == "__main__":
    unittest.main()
