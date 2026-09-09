"""M1 tests: live UI fan-out over /ws (per-chunk delta events, dock activity, new-client broadcast)."""

from __future__ import annotations

import logging
import threading
import time
import unittest

from fastapi.testclient import TestClient

try:  # package form (unittest discover)
    from .mock_upstream import start_mock, stop_mock
except ImportError:  # direct execution fallback
    from mock_upstream import start_mock, stop_mock  # type: ignore

from llm_proxy.app import create_app
from llm_proxy.config import Settings

# Settings for the app under test: the local mock upstream, quiet logs (tests
# that assert on logs capture their own handler).
BASE = Settings(upstream_base_url="http://127.0.0.1:8082", log_level="critical")

MOCK_PORT = 8082
CHAT = "/v1/chat/completions"
CID = "testclient"  # the source host Starlette's TestClient presents to the proxy


def _payload(stream: bool) -> dict:
    return {"model": "local-model", "stream": stream, "messages": [{"role": "user", "content": "hi"}]}


def _receive_json(ws, timeout: float = 3.0) -> dict:
    """Bounded WS receive. A starlette TestClient read blocks forever when the
    expected event never arrives, which would hang the whole suite on a bug."""
    outcome: dict = {}

    def _read() -> None:
        try:
            outcome["ev"] = ws.receive_json()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            outcome["exc"] = exc

    worker = threading.Thread(target=_read, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError(f"no WS event within {timeout:.0f}s")
    if "exc" in outcome:
        raise outcome["exc"]
    return outcome["ev"]


def _read_until_completed(ws, timeout: float = 5.0) -> list[dict]:
    events = []
    deadline = time.monotonic() + timeout
    while True:
        try:
            ev = _receive_json(ws, max(0.1, deadline - time.monotonic()))
        except AssertionError:
            raise AssertionError(
                f"exchange_completed not seen within {timeout:.0f}s; got {[e['type'] for e in events]}"
            ) from None
        events.append(ev)
        if ev["type"] == "exchange_completed":
            return events


class TestLiveFanOut(unittest.TestCase):
    """Each test uses a fresh proxy app (isolated store + hub) against the shared mock."""

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

    def test_streaming_fan_out_is_live(self):
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.client.post(CHAT, json=_payload(stream=True))
            events = _read_until_completed(ws)

        types = [e["type"] for e in events]
        self.assertEqual(types[0], "client_seen")
        self.assertIn("exchange_started", types)
        self.assertIn("delta", types)
        self.assertEqual(types[-1], "exchange_completed")

        # Deltas are live content pieces that reassemble to the full answer.
        deltas = "".join(e["delta"] for e in events if e["type"] == "delta")
        self.assertEqual(deltas, "Hello, world!")

        # Every event is scoped to the same conversation/exchange.
        self.assertTrue(all(e["conversation_id"] == CID for e in events))
        ex_ids = {
            e.get("exchange_id")
            for e in events
            if e["type"] in ("exchange_started", "delta", "exchange_completed")
        }
        self.assertEqual(len(ex_ids), 1)

        # The completed exchange carries the reassembled answer.
        comp = events[-1]["exchange"]
        self.assertEqual(
            comp["server_response"]["reassembled"]["choices"][0]["message"]["content"], "Hello, world!"
        )
        self.assertTrue(comp["server_response"]["streaming"])

    def test_non_streaming_fan_out_has_no_deltas(self):
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.client.post(CHAT, json=_payload(stream=False))
            events = _read_until_completed(ws)

        types = [e["type"] for e in events]
        self.assertIn("exchange_started", types)
        self.assertNotIn("delta", types)
        self.assertEqual(types[-1], "exchange_completed")

        comp = events[-1]["exchange"]
        self.assertEqual(
            comp["server_response"]["body_json"]["choices"][0]["message"]["content"], "Hello, world!"
        )

    def test_unfocused_client_gets_dock_activity_only(self):
        with self.client.websocket_connect("/ws") as ws:
            # No subscribe -> this socket is a dock-only observer.
            self.client.post(CHAT, json=_payload(stream=True))
            # The observer receives exactly three events: the global client_seen
            # broadcast, then lightweight activity pings for exchange_started and
            # exchange_completed (deltas are focus-only). TestClient buffers all of
            # these by the time post() returns, so read the known complete set.
            seen = [_receive_json(ws) for _ in range(3)]

        # A dock observer receives global broadcasts (client_seen) + lightweight
        # activity pings, but never full per-exchange events.
        self.assertEqual(seen[0]["type"], "client_seen")
        allowed = {"activity", "client_seen"}
        self.assertTrue(
            all(e["type"] in allowed for e in seen), f"unexpected types: {[e['type'] for e in seen]}"
        )
        self.assertNotIn("exchange_started", [e["type"] for e in seen])
        self.assertNotIn("delta", [e["type"] for e in seen])
        self.assertNotIn("exchange_completed", [e["type"] for e in seen])
        self.assertTrue(any(e["conversation_id"] == CID for e in seen))

    def test_new_client_seen_reaches_unfocused_dock(self):
        # Regression: a brand-new client must appear in the dock live. client_seen
        # is global, so an unfocused (dock-only) socket receives it even though it
        # is not focused on the new conversation (previously it was dropped).
        with self.client.websocket_connect("/ws") as ws:
            # No subscribe -> dock-only observer.
            self.client.post(CHAT, json=_payload(stream=True))
            ev = _receive_json(ws)
            self.assertEqual(ev["type"], "client_seen")
            self.assertEqual(ev["conversation_id"], CID)

    def test_ui_assets_served(self):
        # The M1 SPA is served from the same static mount as the index.
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('src="/app.js"', r.text)
        self.assertIn('href="/styles.css"', r.text)
        for asset in ("/app.js", "/styles.css"):
            r = self.client.get(asset)
            self.assertEqual(r.status_code, 200, asset)
            self.assertTrue(len(r.content) > 0, asset)
            # UI assets must not be heuristically cached by browsers, or a proxy
            # restart can leave a tab running stale JS against the new process.
            self.assertEqual(r.headers.get("cache-control"), "no-cache", asset)

    def test_focus_switch_routes_events(self):
        with self.client.websocket_connect("/ws") as ws:
            # Focus a conversation we will not use -> must receive no full events for CID.
            ws.send_json({"type": "subscribe", "conversation_id": "somebody-else"})
            self.client.post(CHAT, json=_payload(stream=False))
            # Then switch focus and confirm live events arrive for CID.
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            time.sleep(0.05)  # let the app loop process the focus change before emitting
            self.client.post(CHAT, json=_payload(stream=True))
            events = _read_until_completed(ws)

        # After the focus switch we should get the CID exchange's full events.
        self.assertTrue(all(e["conversation_id"] == CID for e in events))
        self.assertEqual(events[-1]["type"], "exchange_completed")

    def test_reasoning_streams_before_content(self):
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            payload = {"model": "thinker", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
            self.client.post(CHAT, json=payload)
            events = _read_until_completed(ws)

        deltas = [e for e in events if e["type"] == "delta"]
        self.assertTrue(deltas)
        first_content = next(i for i, e in enumerate(deltas) if e["delta"])
        # Reasoning deltas must arrive before the first content delta.
        self.assertTrue(any(e["reasoning_delta"] for e in deltas[:first_content]))
        self.assertEqual("".join(e["delta"] for e in deltas), "Hi!")
        self.assertEqual("".join(e.get("reasoning_delta", "") for e in deltas), "Let me think...")

        comp = events[-1]["exchange"]
        msg = comp["server_response"]["reassembled"]["choices"][0]["message"]
        self.assertEqual(msg["content"], "Hi!")
        self.assertEqual(msg["reasoning_content"], "Let me think...")
        self.assertIsNotNone(comp["timings"]["t_first_content"])
        self.assertEqual(comp["timings"]["gen_tok_per_sec"], 200.0)

    def test_upstream_gen_rate_captured_stream_and_non_stream(self):
        # Stream: upstream timings arrive in the final SSE chunk.
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.client.post(CHAT, json=_payload(stream=True))
            stream_ex = _read_until_completed(ws)[-1]["exchange"]
        self.assertEqual(stream_ex["timings"]["gen_tok_per_sec"], 100.0)

        # Non-stream: upstream timings arrive in the JSON body (the byte-level
        # window is useless here, so this is the only sane tok/s source).
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.client.post(CHAT, json=_payload(stream=False))
            non_ex = _read_until_completed(ws)[-1]["exchange"]
        self.assertEqual(non_ex["timings"]["gen_tok_per_sec"], 100.0)
        self.assertIsNone(non_ex["timings"]["t_first_content"])

    def test_client_seen_emit_is_logged_at_info(self):
        """Regression: the client_seen broadcast fan-out must be visible at INFO.

        "Empty dock despite a live WS" was undiagnosable until this line existed:
        "-> N full" proves delivery to the UI's queue; "dropped" (WARNING) proves
        the UI was not connected when the client appeared.
        """
        logger = logging.getLogger("llm_proxy.ws")
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        capture = _Capture()
        old_level = logger.level
        logger.addHandler(capture)
        logger.setLevel(logging.INFO)  # the production default
        try:
            with self.client.websocket_connect("/ws") as ws:
                self.client.post(CHAT, json=_payload(stream=False))
                ev = _receive_json(ws)
                self.assertEqual(ev["type"], "client_seen")
        finally:
            logger.removeHandler(capture)
            logger.setLevel(old_level)

        text = "\n".join(r.getMessage() for r in records)
        self.assertIn("emit client_seen -> 1 full", text)

    def test_client_registered_before_client_seen_broadcast(self):
        """Regression: by the time client_seen is broadcast, the client must
        already be in the store.

        The UI re-fetches /api/clients the instant it sees client_seen. If the
        store does not yet contain the client (it used to be registered later,
        inside _capture after the upstream round-trip), that re-fetch returns an
        empty dock - and client_seen only fires once, so the dock stays empty
        until a manual refresh. Registering before the emit closes the window.
        """
        store = self.client.app.state.store
        hub = self.client.app.state.hub
        real_emit = hub.emit
        violations: list[str] = []

        def spy_emit(conversation_id, event, **kwargs):
            if event.get("type") == "client_seen" and not store.has_client(event["client_id"]):
                violations.append(event["client_id"])
            real_emit(conversation_id, event, **kwargs)

        hub.emit = spy_emit
        try:
            with self.client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "subscribe", "conversation_id": CID})
                time.sleep(0.05)  # let the app loop process the focus change before emitting
                self.client.post(CHAT, json=_payload(stream=False))
                _read_until_completed(ws)
        finally:
            hub.emit = real_emit

        self.assertEqual(violations, [])

    def test_ws_lifecycle_is_logged(self):
        """Regression: WS connect/focus/disconnect must be visible in the server log.

        The UI's "ws:on but no client in the dock" failure mode is undiagnosable
        without a connection trace, so capture the llm_proxy.ws logger output and
        assert the lifecycle lines appear.
        """
        logger = logging.getLogger("llm_proxy.ws")
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        capture = _Capture()
        old_level = logger.level
        logger.addHandler(capture)
        logger.setLevel(logging.DEBUG)
        try:
            with self.client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "subscribe", "conversation_id": CID})
                time.sleep(0.05)  # let the app loop process the focus change
            time.sleep(0.05)  # TestClient close -> server-side disconnect is racy
        finally:
            logger.removeHandler(capture)
            logger.setLevel(old_level)

        text = "\n".join(r.getMessage() for r in records)
        self.assertIn("ws connected", text)
        self.assertIn("ws focus", text)
        self.assertIn(CID, text)  # focus was set to the test client's conversation
        self.assertIn("ws disconnected", text)
