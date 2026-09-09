"""M5 end-to-end tests: the transparent catch-all proxy and the two-port split.

The LLM listener forwards *every* method/path verbatim (query string included)
and taps the response opaquely; the UI listener serves no proxy routes at all.
"""

from __future__ import annotations

import unittest

try:  # package form (unittest discover)
    from .helpers import clients, close
    from .mock_upstream import start_mock, stop_mock
except ImportError:  # direct execution fallback
    from helpers import clients, close  # type: ignore
    from mock_upstream import start_mock, stop_mock  # type: ignore

from llm_proxy.app import cli_overrides
from llm_proxy.config import Settings

BASE = Settings(upstream_base_url="http://127.0.0.1:8082", log_level="critical")

MOCK_PORT = 8082
CID = "testclient"  # the source host Starlette's TestClient presents to the proxy


class TestCliPorts(unittest.TestCase):
    """--llm-port/--ui-port map onto the two listener ports."""

    def test_two_port_overrides(self):
        self.assertEqual(
            cli_overrides(["http://x", "--llm-port", "8081", "--ui-port", "9091"]),
            {"upstream_base_url": "http://x", "llm_port": 8081, "ui_port": 9091},
        )


class TestTransparent(unittest.TestCase):
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

    def _exchanges(self) -> list[dict]:
        """The (single) testclient's exchanges, read back through the UI API."""
        body = self.ui.get("/api/clients").json()
        c = next(x for x in body if x["id"] == CID)
        conv = self.ui.get(f"/api/conversations/{c['conversation_ids'][0]}").json()
        return conv["exchanges"]

    def test_query_string_forwarded_verbatim(self):
        # The proxy must not drop the query string: /props?model=... is how
        # llama.cpp clients probe the upstream (the Zed case that motivated M5).
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


if __name__ == "__main__":
    unittest.main()
