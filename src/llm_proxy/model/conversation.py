"""Conversation objects: Client, Exchange, and a bounded ring buffer of exchanges.

In-memory only for v1 (SQLite is roadmap). A single asyncio event loop owns these,
so no locks are needed on the hot path.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Client:
    id: str
    name: str = ""
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


@dataclass
class Exchange:
    id: str
    sequence: int = 0
    is_replay: bool = False
    client_request: dict | None = None
    server_response: dict | None = None
    timings: dict | None = None
    usage: dict | None = None
    error: dict | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "is_replay": self.is_replay,
            "client_request": self.client_request,
            "server_response": self.server_response,
            "timings": self.timings,
            "usage": self.usage,
            "error": self.error,
        }


class Conversation:
    """A per-client conversation: a bounded, time-capped ring buffer of exchanges."""

    def __init__(
        self,
        id: str,
        client_id: str,
        tag: str = "",
        max_exchanges: int = 500,
        max_age_hours: int = 24,
    ):
        self.id = id
        self.client_id = client_id
        self.tag = tag
        self.created_at = time.time()
        self._max_age_s = max_age_hours * 3600
        self._seq = 0
        self._exchanges: deque[Exchange] = deque(maxlen=max_exchanges)

    def append(self, exchange: Exchange) -> None:
        self._evict_old()
        exchange.sequence = self._seq
        self._seq += 1
        self._exchanges.append(exchange)

    def _evict_old(self) -> None:
        cutoff = time.time() - self._max_age_s
        while self._exchanges and self._exchanges[0].created_at < cutoff:
            self._exchanges.popleft()

    @property
    def exchanges(self) -> list[Exchange]:
        return list(self._exchanges)
