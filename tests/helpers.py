"""Shared test helpers: app/client builders, the shared mock-upstream fixture,
and constants used across the suite.

The proxy runs one process with two listeners (LLM proxy + UI). Tests build both
apps against the *same* Context so the store and hub are shared (the point of a
single process); each listener builds its own upstream client + pipeline in its
lifespan. The LLM client blanks the User-Agent so its id stays "testclient"; the
UI client makes no proxy calls.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

try:  # package form (unittest discover)
    from .mock_upstream import start_mock, stop_mock
except ImportError:  # direct execution fallback
    from mock_upstream import start_mock, stop_mock  # type: ignore

from llm_proxy.app import Context, create_llm_app, create_ui_app
from llm_proxy.config import Settings

# Settings for the apps under test: the local mock upstream, quiet logs (tests
# that assert on logs capture their own handler).
BASE = Settings(upstream_base_url="http://127.0.0.1:8082", log_level="critical")

MOCK_PORT = 8082
CHAT = "/v1/chat/completions"
CID = "testclient"  # the source host Starlette's TestClient presents to the proxy


def payload(stream: bool, model: str = "local-model") -> dict:
    """A minimal chat-completions body; the mock answers per model name."""
    return {"model": model, "stream": stream, "messages": [{"role": "user", "content": "hi"}]}


def clients(settings: Settings) -> tuple[Context, TestClient, TestClient]:
    """A fresh shared Context plus entered LLM and UI TestClients.

    Returns ``(ctx, llm, ui)``. Call :func:`close` in ``tearDown`` (each client
    closes its own upstream client via its lifespan).
    """
    ctx = Context(settings)
    llm = TestClient(create_llm_app(ctx), headers={"User-Agent": ""})
    ui = TestClient(create_ui_app(ctx))
    llm.__enter__()
    ui.__enter__()
    return ctx, llm, ui


def close(llm: TestClient, ui: TestClient) -> None:
    llm.__exit__(None, None, None)
    ui.__exit__(None, None, None)


def ui_client(**overrides) -> TestClient:
    """An entered UI TestClient with a fresh Context; ``overrides`` are Settings
    fields on top of :data:`BASE` (no LLM listener, no proxy calls)."""
    client = TestClient(create_ui_app(Context(BASE.model_copy(update=overrides))))
    client.__enter__()
    return client


def close_ui(ui: TestClient) -> None:
    ui.__exit__(None, None, None)


class MockedCase(unittest.TestCase):
    """A fresh shared Context (isolated store + hub) per test, against the shared
    mock upstream (started/stopped once per test class)."""

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
