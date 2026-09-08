"""M0 end-to-end tests: the proxy running against the mock llama.cpp upstream."""

from __future__ import annotations

import os
import socket
import unittest

# Point the proxy at the local mock BEFORE settings are read.
os.environ["UPSTREAM_BASE_URL"] = "http://127.0.0.1:8082"
os.environ["LISTEN_PORT"] = "9090"

from fastapi.testclient import TestClient  # noqa: E402

try:  # package form (unittest discover)
    from .mock_upstream import start_mock, stop_mock
except ImportError:  # direct execution fallback
    from mock_upstream import start_mock, stop_mock  # type: ignore  # noqa: E402

from llm_proxy.app import create_app  # noqa: E402
from llm_proxy.config import get_settings  # noqa: E402

MOCK_PORT = 8082
CHAT = "/v1/chat/completions"


def _payload(stream: bool) -> dict:
    return {"model": "local-model", "stream": stream, "messages": [{"role": "user", "content": "hi"}]}


class TestProxy(unittest.TestCase):
    """Each test uses a fresh proxy app (isolated in-memory store) against the shared mock."""

    @classmethod
    def setUpClass(cls):
        start_mock(MOCK_PORT)

    @classmethod
    def tearDownClass(cls):
        try:
            stop_mock()
        except Exception:
            pass

    def setUp(self):
        self.client = TestClient(create_app())
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["upstream"], "ok")

    def test_health_reports_upstream_error_when_unreachable(self):
        # /health must report the *link* state, not just the proxy process: a
        # dead upstream shows up as upstream=error (the UI pill turns red).
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()

        old = os.environ.get("UPSTREAM_BASE_URL")
        os.environ["UPSTREAM_BASE_URL"] = f"http://127.0.0.1:{dead_port}"
        get_settings.cache_clear()
        try:
            with TestClient(create_app()) as c:
                body = c.get("/health").json()
                self.assertEqual(body["status"], "ok")
                self.assertEqual(body["upstream"], "error")
        finally:
            if old is None:
                os.environ.pop("UPSTREAM_BASE_URL", None)
            else:
                os.environ["UPSTREAM_BASE_URL"] = old
            get_settings.cache_clear()

    def test_non_streaming_passthrough(self):
        r = self.client.post(CHAT, json=_payload(stream=False))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello, world!")
        self.assertEqual(body["usage"]["total_tokens"], 8)

    def test_streaming_tap_and_forward(self):
        r = self.client.post(CHAT, json=_payload(stream=True))
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers.get("content-type", ""))
        self.assertIn("[DONE]", r.text)
        self.assertIn("Hello", r.text)

    def test_streaming_upstream_error_status_passthrough(self):
        # Upstream returns a JSON error (not SSE) for a stream request; the proxy
        # must surface the upstream's status, not a premature 200.
        r = self.client.post(
            CHAT, json={"model": "boom", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(r.status_code, 404)
        self.assertIn("model not found", r.json()["error"]["message"])

    def test_streaming_upstream_unreachable_returns_502(self):
        # Upstream unreachable (connection refused) -> a clean 502, not a phantom 200.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()

        old = os.environ.get("UPSTREAM_BASE_URL")
        os.environ["UPSTREAM_BASE_URL"] = f"http://127.0.0.1:{dead_port}"
        get_settings.cache_clear()
        try:
            with TestClient(create_app()) as c:
                r = c.post(CHAT, json=_payload(stream=True))
                self.assertEqual(r.status_code, 502)
                self.assertIn("upstream error", r.json()["error"]["message"])
        finally:
            if old is None:
                os.environ.pop("UPSTREAM_BASE_URL", None)
            else:
                os.environ["UPSTREAM_BASE_URL"] = old
            get_settings.cache_clear()

    def test_capture_and_export(self):
        self.client.post(CHAT, json=_payload(stream=False))
        self.client.post(CHAT, json=_payload(stream=True))

        clients = self.client.get("/api/clients").json()
        self.assertTrue(clients)
        cid = clients[0]["conversation_ids"][0]

        conv = self.client.get(f"/api/conversations/{cid}").json()
        self.assertGreaterEqual(conv["exchange_count"], 2)

        stream_ex = [ex for ex in conv["exchanges"] if ex["server_response"]["streaming"]]
        self.assertTrue(stream_ex)
        content = stream_ex[0]["server_response"]["reassembled"]["choices"][0]["message"]["content"]
        self.assertEqual(content, "Hello, world!")
        self.assertEqual(stream_ex[0]["usage"]["total_tokens"], 8)

        exp = self.client.get(f"/api/conversations/{cid}/export").json()
        self.assertEqual(exp["format"], "llm-proxy/conversation")
        self.assertEqual(exp["version"], 1)
        self.assertGreaterEqual(len(exp["exchanges"]), 2)
        self.assertGreaterEqual(exp["stats"]["total_completion_tokens"], 3)

    def test_serves_ui_and_favicon(self):
        # The status page is served from the ui/ dir and must not contain
        # placeholder conversation links (literal <id> hrefs -> junk 404s).
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/favicon.svg"', r.text)
        self.assertNotIn('href="/api/conversations', r.text)

        # Browsers probe /favicon.ico even with an SVG icon link; serve the SVG.
        r = self.client.get("/favicon.ico")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/svg+xml")
        self.assertIn("<svg", r.text)

    def test_secret_redaction(self):
        self.client.post(
            CHAT, json=_payload(stream=False), headers={"Authorization": "Bearer sk-secret-test-key-123"}
        )
        clients = self.client.get("/api/clients").json()
        cid = clients[0]["conversation_ids"][0]
        conv = self.client.get(f"/api/conversations/{cid}").json()
        auth = conv["exchanges"][0]["client_request"]["headers"].get("authorization")
        self.assertIsNotNone(auth)
        self.assertIn("***", auth)
        self.assertNotIn("sk-secret-test-key-123", auth)


if __name__ == "__main__":
    unittest.main()
