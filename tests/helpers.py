"""Shared test helpers: build a fresh shared Context + LLM and UI TestClients.

The proxy runs one process with two listeners (LLM proxy + UI). Tests build both
apps against the *same* Context so the store and hub are shared (the point of a
single process); each listener builds its own upstream client + pipeline in its
lifespan. The LLM client blanks the User-Agent so its id stays "testclient"; the
UI client makes no proxy calls.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from llm_proxy.app import Context, create_llm_app, create_ui_app
from llm_proxy.config import Settings


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
