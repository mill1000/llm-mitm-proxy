"""M0 end-to-end tests: the proxy running against the mock llama.cpp upstream."""

from __future__ import annotations

import contextlib
import importlib
import io
import socket
import unittest
from urllib.parse import quote

from fastapi.testclient import TestClient

try:  # package form (unittest discover)
    from .helpers import clients, close
    from .mock_upstream import start_mock, stop_mock
except ImportError:  # direct execution fallback
    from helpers import clients, close  # type: ignore
    from mock_upstream import start_mock, stop_mock  # type: ignore

from llm_proxy.app import Context, cli_overrides, create_llm_app, create_ui_app
from llm_proxy.config import Settings

# Settings for the app under test: the local mock upstream, quiet logs (tests
# that assert on logs capture their own handler).
BASE = Settings(upstream_base_url="http://127.0.0.1:8082", log_level="critical")

MOCK_PORT = 8082
CHAT = "/v1/chat/completions"


def _payload(stream: bool) -> dict:
    return {"model": "local-model", "stream": stream, "messages": [{"role": "user", "content": "hi"}]}


class TestPackaging(unittest.TestCase):
    """__version__ must come from the installed package metadata (setuptools_scm), not a fallback."""

    def test_version_matches_installed_metadata(self):
        from importlib.metadata import version as pkg_version

        from llm_proxy import __version__

        self.assertEqual(__version__, pkg_version("llm-proxy"))

    def test_console_script_resolves(self):
        """The llm-proxy console script must resolve to a callable in the package."""
        from importlib.metadata import distribution

        eps = [ep for ep in distribution("llm-proxy").entry_points if ep.name == "llm-proxy"]
        self.assertEqual(len(eps), 1)
        module_name, _, attr = eps[0].value.partition(":")
        module = importlib.import_module(module_name)
        self.assertTrue(callable(getattr(module, attr)))


class TestCli(unittest.TestCase):
    """cli_overrides() maps command-line args onto Settings field names."""

    def test_no_args_no_overrides(self):
        self.assertEqual(cli_overrides([]), {})

    def test_positional_upstream_and_flags(self):
        self.assertEqual(
            cli_overrides(["http://127.0.0.1:8080", "--host", "0.0.0.0", "--ui-port", "9091"]),
            {"upstream_base_url": "http://127.0.0.1:8080", "listen_host": "0.0.0.0", "ui_port": 9091},
        )

    def test_help_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli_overrides(["--help"])
        self.assertEqual(cm.exception.code, 0)


class TestProxy(unittest.TestCase):
    """Each test uses a fresh shared Context (isolated store) against the shared mock."""

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
        self.ctx, self.llm, self.ui = clients(BASE)

    def tearDown(self):
        close(self.llm, self.ui)

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
        r = self.llm.post(CHAT, json=_payload(stream=False))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello, world!")
        self.assertEqual(body["usage"]["total_tokens"], 8)

    def test_streaming_tap_and_forward(self):
        r = self.llm.post(CHAT, json=_payload(stream=True))
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers.get("content-type", ""))
        self.assertIn("[DONE]", r.text)
        self.assertIn("Hello", r.text)

    def test_streaming_upstream_error_status_passthrough(self):
        # Upstream returns a JSON error (not SSE) for a stream request; the proxy
        # must surface the upstream's status, not a premature 200.
        r = self.llm.post(
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

        dead = BASE.model_copy(update={"upstream_base_url": f"http://127.0.0.1:{dead_port}"})
        with TestClient(create_llm_app(Context(dead))) as c:
            r = c.post(CHAT, json=_payload(stream=True))
            self.assertEqual(r.status_code, 502)
            self.assertIn("upstream error", r.json()["error"]["message"])

    def test_capture_and_export(self):
        self.llm.post(CHAT, json=_payload(stream=False))
        self.llm.post(CHAT, json=_payload(stream=True))

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
        self.assertEqual(exp["format"], "llm-proxy/conversation")
        self.assertEqual(exp["version"], 1)
        self.assertGreaterEqual(len(exp["exchanges"]), 2)
        self.assertGreaterEqual(exp["stats"]["total_completion_tokens"], 3)

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

    def test_secret_redaction(self):
        self.llm.post(
            CHAT, json=_payload(stream=False), headers={"Authorization": "Bearer sk-secret-test-key-123"}
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
            self.llm.post(CHAT, json=_payload(stream=False), headers={"User-Agent": ua})

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
            json=_payload(stream=False),
            headers={"User-Agent": "curl/8.5.0", "Authorization": "Bearer sk-test"},
        )
        clients_body = self.ui.get("/api/clients").json()
        self.assertEqual(len(clients_body), 1)
        self.assertTrue(clients_body[0]["id"].endswith("::curl_8.5.0::sk-test"))


if __name__ == "__main__":
    unittest.main()
