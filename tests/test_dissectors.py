"""Dissectors: chat-completions decode (openai) vs the generic raw-capture
fallback, selected per request; plus the last-user preview helper."""

from __future__ import annotations

import unittest

try:  # package form (unittest discover)
    from .helpers import CHAT, CID, MockedCase, payload
except ImportError:  # direct execution fallback
    from helpers import CHAT, CID, MockedCase, payload  # type: ignore

from llm_proxy.dissectors.openai import _last_user_preview


class TestDecode(MockedCase):
    """Per-request decode: chat -> openai decoder, non-chat -> generic fallback."""

    def _exchanges(self) -> list[dict]:
        """The testclient's exchanges, read back through the UI API."""
        return self.ui.get(f"/api/conversations/{CID}").json()["exchanges"]

    def test_chat_decoded_as_openai(self):
        self.llm.post(CHAT, json=payload(stream=True))
        (ex,) = self._exchanges()
        self.assertEqual(ex["dissector"], "openai")
        self.assertEqual(
            ex["server_response"]["reassembled"]["choices"][0]["message"]["content"], "Hello, world!"
        )

    def test_nonchat_falls_back_to_generic(self):
        self.llm.get("/props", params={"model": "m"})
        self.llm.get("/v1/models")
        self.llm.get("/nope")
        exs = self._exchanges()
        for ex in exs:
            self.assertEqual(ex["dissector"], "generic")
        self.assertEqual(exs[0]["server_response"]["body_json"], {"n_ctx": 4096, "model": "m"})
        self.assertEqual(exs[1]["server_response"]["body_json"]["object"], "list")
        self.assertEqual(exs[2]["server_response"]["status"], 404)

    def test_mixed_conversation(self):
        self.llm.post(CHAT, json=payload(stream=True))
        self.llm.get("/props", params={"model": "m"})
        exs = self._exchanges()
        self.assertEqual(exs[0]["dissector"], "openai")
        self.assertIn("reassembled", exs[0]["server_response"])
        self.assertEqual(exs[1]["dissector"], "generic")
        self.assertEqual(exs[1]["server_response"]["body_json"], {"n_ctx": 4096, "model": "m"})


class TestPreview(unittest.TestCase):
    """The last-user preview is capped and always marks truncation."""

    def test_short_string_untouched(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(_last_user_preview(body), "hi")

    def test_long_string_truncated_with_ellipsis(self):
        body = {"messages": [{"role": "user", "content": "x" * 600}]}
        p = _last_user_preview(body)
        self.assertEqual(len(p), 501)  # 500 + ellipsis
        self.assertTrue(p.endswith("…"))

    def test_list_content_truncated_with_ellipsis(self):
        body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "y" * 600}]}]}
        p = _last_user_preview(body)
        self.assertTrue(p.endswith("…"))
        self.assertEqual(len(p), 501)

    def test_skips_non_user_roles(self):
        body = {"messages": [{"role": "tool", "content": "z" * 600}]}
        self.assertIsNone(_last_user_preview(body))


if __name__ == "__main__":
    unittest.main()
