"""WebSocket live UI: per-chunk delta fan-out, focus routing, dock activity,
new-client broadcast, and liveness (ping/pong + dead-socket pruning). Each fan-out
test uses a fresh shared Context (isolated store + hub) against the shared mock
upstream; liveness tests build a UI-only client with fast pings."""

from __future__ import annotations

import json
import logging
import threading
import time
import unittest

try:  # package form (unittest discover)
    from .helpers import CHAT, CID, MockedCase, close_ui, payload, ui_client
except ImportError:  # direct execution fallback
    from helpers import (  # type: ignore
        CHAT,
        CID,
        MockedCase,
        close_ui,
        payload,
        ui_client,
    )


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


class TestLiveFanOut(MockedCase):
    def test_streaming_fan_out_is_live(self):
        with self.ui.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.llm.post(CHAT, json=payload(stream=True))
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
        with self.ui.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.llm.post(CHAT, json=payload(stream=False))
            events = _read_until_completed(ws)

        types = [e["type"] for e in events]
        self.assertIn("exchange_started", types)
        self.assertNotIn("delta", types)
        self.assertEqual(types[-1], "exchange_completed")

        comp = events[-1]["exchange"]
        self.assertEqual(
            comp["server_response"]["body_json"]["choices"][0]["message"]["content"], "Hello, world!"
        )

    def test_exchange_started_carries_server_sequence(self):
        """Regression: exchange_started must carry the server-assigned sequence.

        The UI used to guess the pending card's sequence (lastSeq + 1), which
        desyncs from the real sequence once a slow in-flight request sits behind
        completed ones. A replay from such a pending card then re-sends to the wrong
        exchange's endpoint. The server assigns the sequence at begin_exchange, so
        forwarding it in the event makes the pending card (and any replay from it)
        target the right exchange.
        """
        slow_payload = payload(stream=False, model="slow")
        with self.ui.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            # A fast request completes first (real sequence 0).
            self.llm.get("/v1/models")
            # A slow request stays in flight long enough to observe its placeholder.
            slow = threading.Thread(
                target=self.llm.post, kwargs={"url": CHAT, "json": slow_payload}, daemon=True
            )
            slow.start()
            time.sleep(0.3)  # let the fast exchange complete and the slow one go in flight
            exchanges = self.ui.get(f"/api/conversations/{CID}").json()["exchanges"]
            in_flight = next(e for e in exchanges if e["in_flight"])
            stored_seq, slow_id = in_flight["sequence"], in_flight["id"]
            for _ in range(20):
                ev = _receive_json(ws, 3.0)
                if ev["type"] == "exchange_started" and ev["exchange_id"] == slow_id:
                    break
            else:
                self.fail("exchange_started for the in-flight exchange never arrived")
            self.assertEqual(ev["sequence"], stored_seq)
            slow.join(timeout=5)

    def test_completed_sequence_matches_stored_order(self):
        """Regression: exchange_completed must carry each request's real sequence.

        _capture used to build the completed exchange with sequence=0, so every
        completed card showed "#0" and a replay from it resolved to sequence 0 (the
        first request). The store finalizes the in-flight placeholder in place and
        returns it; the pipeline must emit that copy so the sequence stays the one
        assigned at begin_exchange. Models the exact repro: models, chat, models.
        """
        with self.ui.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.llm.get("/v1/models")  # sequence 0
            self.llm.post(CHAT, json=payload(stream=False))  # sequence 1
            self.llm.get("/v1/models")  # sequence 2
            completed = []
            while len(completed) < 3:
                ev = _receive_json(ws)
                if ev["type"] == "exchange_completed":
                    completed.append(ev["exchange"])
        self.assertEqual([e["sequence"] for e in completed], [0, 1, 2])
        self.assertEqual(
            [(e["client_request"] or {}).get("path") for e in completed],
            ["/v1/models", CHAT, "/v1/models"],
        )

    def test_unfocused_client_gets_dock_activity_only(self):
        with self.ui.websocket_connect("/ws") as ws:
            # No subscribe -> this socket is a dock-only observer.
            self.llm.post(CHAT, json=payload(stream=True))
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
        with self.ui.websocket_connect("/ws") as ws:
            # No subscribe -> dock-only observer.
            self.llm.post(CHAT, json=payload(stream=True))
            ev = _receive_json(ws)
            self.assertEqual(ev["type"], "client_seen")
            self.assertEqual(ev["conversation_id"], CID)

    def test_focus_switch_routes_events(self):
        with self.ui.websocket_connect("/ws") as ws:
            # Focus a conversation we will not use -> must receive no full events for CID.
            ws.send_json({"type": "subscribe", "conversation_id": "somebody-else"})
            self.llm.post(CHAT, json=payload(stream=False))
            # Then switch focus and confirm live events arrive for CID.
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            time.sleep(0.05)  # let the app loop process the focus change before emitting
            self.llm.post(CHAT, json=payload(stream=True))
            events = _read_until_completed(ws)

        # After the focus switch we should get the CID exchange's full events.
        self.assertTrue(all(e["conversation_id"] == CID for e in events))
        self.assertEqual(events[-1]["type"], "exchange_completed")

    def test_reasoning_streams_before_content(self):
        with self.ui.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.llm.post(CHAT, json=payload(stream=True, model="thinker"))
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

    def test_tool_calls_reassembled(self):
        # Agentic replies: fragmented delta.tool_calls must reassemble into the
        # message (id/type/name from the first fragment, arguments concatenated).
        with self.ui.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.llm.post(CHAT, json=payload(stream=True, model="tooler"))
            events = _read_until_completed(ws)

        comp = events[-1]["exchange"]
        msg = comp["server_response"]["reassembled"]["choices"][0]["message"]
        self.assertEqual(msg["content"], "Let me check.")
        self.assertEqual(comp["server_response"]["reassembled"]["choices"][0]["finish_reason"], "tool_calls")
        (tc,) = msg["tool_calls"]
        self.assertEqual(tc["id"], "call_1")
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "bash")
        self.assertEqual(tc["function"]["arguments"], '{"command": "ls"}')

    def test_upstream_gen_rate_captured_stream_and_non_stream(self):
        # Stream: upstream timings arrive in the final SSE chunk.
        with self.ui.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.llm.post(CHAT, json=payload(stream=True))
            stream_ex = _read_until_completed(ws)[-1]["exchange"]
        self.assertEqual(stream_ex["timings"]["gen_tok_per_sec"], 100.0)

        # Non-stream: upstream timings arrive in the JSON body (the byte-level
        # window is useless here, so this is the only sane tok/s source).
        with self.ui.websocket_connect("/ws") as ws:
            ws.send_json({"type": "subscribe", "conversation_id": CID})
            self.llm.post(CHAT, json=payload(stream=False))
            non_ex = _read_until_completed(ws)[-1]["exchange"]
        self.assertEqual(non_ex["timings"]["gen_tok_per_sec"], 100.0)
        self.assertIsNone(non_ex["timings"]["t_first_content"])

    def test_client_registered_before_client_seen_broadcast(self):
        """Regression: by the time client_seen is broadcast, the client must
        already be in the store.

        The UI re-fetches /api/clients the instant it sees client_seen. If the
        store does not yet contain the client (it used to be registered later,
        inside _capture after the upstream round-trip), that re-fetch returns an
        empty dock - and client_seen only fires once, so the dock stays empty
        until a manual refresh. Registering before the emit closes the window.
        """
        store = self.ui.app.state.store
        hub = self.ui.app.state.hub
        real_emit = hub.emit
        violations: list[str] = []

        def spy_emit(conversation_id, event, **kwargs):
            if event.get("type") == "client_seen" and not store.has_client(event["client_id"]):
                violations.append(event["client_id"])
            real_emit(conversation_id, event, **kwargs)

        hub.emit = spy_emit
        try:
            with self.ui.websocket_connect("/ws") as ws:
                ws.send_json({"type": "subscribe", "conversation_id": CID})
                time.sleep(0.05)  # let the app loop process the focus change before emitting
                self.llm.post(CHAT, json=payload(stream=False))
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
            with self.ui.websocket_connect("/ws") as ws:
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


class TestWsLiveness(unittest.TestCase):
    """Liveness with fast pings (0.3s interval/timeout): a ponging socket keeps
    receiving pings, a silent one is closed with 1011 and pruned from the hub.

    With interval=timeout=0.3 the pinger checks at ~0.3s, ~0.6s, ~0.9s: a
    silent socket is closed at the first check where silence > 0.6s (~0.9s),
    and a ponging socket survives because its last_pong stays fresh.
    """

    def setUp(self):
        self.client = ui_client(ws_ping_interval=0.3, ws_ping_timeout=0.3)

    def tearDown(self):
        close_ui(self.client)

    def _receive_ping(self, ws) -> dict:
        """Block for the next frame and assert it is a liveness ping, not a close.

        (Raw ASGI app->client text frames are typed ``websocket.send``.)
        """
        msg = ws.receive()
        self.assertEqual(msg["type"], "websocket.send")
        ev = json.loads(msg["text"])
        self.assertEqual(ev["type"], "ping")
        self.assertIn("ts", ev)
        return ev

    def test_ws_ping_pong_keeps_socket_alive(self):
        hub = self.client.app.state.hub
        with self.client.websocket_connect("/ws") as ws:
            ping1 = self._receive_ping(ws)
            ws.send_json({"type": "pong", "ts": ping1["ts"]})
            ping2 = self._receive_ping(ws)
            ws.send_json({"type": "pong", "ts": ping2["ts"]})
            # A socket that never pongs is closed at this check; a ponging one
            # still receives the next ping.
            self._receive_ping(ws)
            self.assertEqual(len(hub._conns), 1)

    def test_ws_silent_socket_pruned(self):
        hub = self.client.app.state.hub
        with self.client.websocket_connect("/ws") as ws:
            self.assertEqual(len(hub._conns), 1)
            # No pong at all: the pinger sends pings until silence exceeds
            # interval + timeout, then must close the socket with 1011.
            # Bounded: the close arrives within ~3 intervals.
            msg = ws.receive()
            while msg["type"] == "websocket.send":  # skip liveness pings
                msg = ws.receive()
            self.assertEqual(msg["type"], "websocket.close")
            self.assertEqual(msg.get("code"), 1011)
        time.sleep(0.1)  # the endpoint's finally() runs hub.disconnect after the close
        self.assertEqual(len(hub._conns), 0)


if __name__ == "__main__":
    unittest.main()
