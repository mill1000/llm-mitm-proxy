"""SIGTERM shutdown must be bounded even with a request stuck in flight.

Reproduces the production hang: the proxy forwards a request to an upstream that
never answers (persistent /models/sse feeds, long chat streams), so the client
connection lingers past graceful shutdown. The uvicorn wait was unbounded
(``timeout_graceful_shutdown`` unset), so ``docker compose down`` hit its 10s
grace period and SIGKILLed. The wait is now capped at ``SHUTDOWN_GRACE``.
"""

from __future__ import annotations

import http.client
import json
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from llm_proxy.app import SHUTDOWN_GRACE


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _HangingUpstream:
    """Accepts connections and never answers, like a stalled model stream."""

    def __init__(self, port: int) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            conn, _ = self._sock.accept()
            conn.recv(4096)  # swallow the request head, then hold it forever

    def close(self) -> None:
        self._sock.close()


def _tcp_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


@unittest.skipUnless(sys.platform != "win32", "needs SIGTERM")
class ShutdownBoundedTest(unittest.TestCase):
    def test_sigterm_exits_quickly_while_request_in_flight(self) -> None:
        exe = Path(sys.executable).with_name("llm-mitm-proxy")
        if not exe.is_file():
            self.skipTest(f"console script not found: {exe}")
        llm_port, ui_port, up_port = _free_port(), _free_port(), _free_port()
        upstream = _HangingUpstream(up_port)
        proc = subprocess.Popen(
            [
                str(exe),
                f"http://127.0.0.1:{up_port}",
                "--host",
                "127.0.0.1",
                "--proxy-port",
                str(llm_port),
                "--web-port",
                str(ui_port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        client: http.client.HTTPConnection | None = None
        try:
            deadline = time.monotonic() + 10.0
            while not _tcp_up(ui_port):
                self.assertLess(time.monotonic(), deadline, "proxy did not start within 10s")
                time.sleep(0.1)
            # A proxied request the upstream never answers: in flight forever.
            client = http.client.HTTPConnection("127.0.0.1", llm_port)
            body = json.dumps({"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
            client.request(
                "POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"}
            )
            time.sleep(0.3)  # let it reach the proxy and hang on the upstream
            t0 = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            try:
                proc.communicate(timeout=8.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate(timeout=5.0)
                tail = "\n".join(out.splitlines()[-40:])
                self.fail(f"shutdown exceeded 8s (grace={SHUTDOWN_GRACE}s);\nlog tail:\n{tail}")
            elapsed = time.monotonic() - t0
            self.assertEqual(proc.returncode, 0, f"proxy exited with {proc.returncode}")
            self.assertLess(elapsed, SHUTDOWN_GRACE + 2.0, f"shutdown took {elapsed:.1f}s")
        finally:
            if client is not None:
                try:
                    client.close()
                except OSError:
                    pass
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)
            upstream.close()
