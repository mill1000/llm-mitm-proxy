"""In-memory store of clients and their conversations (ring buffers)."""

from __future__ import annotations

import time

from ..model.conversation import Client, Conversation, Exchange


class MemoryStore:
    def __init__(self, max_exchanges: int = 500, max_age_hours: int = 24):
        self._max_exchanges = max_exchanges
        self._max_age_hours = max_age_hours
        self._clients: dict[str, Client] = {}
        self._conversations: dict[str, Conversation] = {}
        self._inflight: dict[str, Exchange] = {}
        self._inflight_cids: dict[str, str] = {}

    @staticmethod
    def _conv_id(client_id: str, tag: str) -> str:
        return f"{client_id}" if not tag else f"{client_id}::{tag}"

    @staticmethod
    def conversation_id(client_id: str, tag: str) -> str:
        """Public accessor so the pipeline can scope WS events to a conversation."""
        return MemoryStore._conv_id(client_id, tag)

    def has_client(self, client_id: str) -> bool:
        return client_id in self._clients

    def get_or_create_conversation(self, client_id: str, tag: str = "") -> tuple[Client, Conversation]:
        cid = self._conv_id(client_id, tag)
        conv = self._conversations.get(cid)
        if conv is None:
            conv = Conversation(cid, client_id, tag, self._max_exchanges, self._max_age_hours)
            self._conversations[cid] = conv
        client = self._clients.get(client_id)
        if client is None:
            client = Client(id=client_id, name=client_id)
            self._clients[client_id] = client
        else:
            client.last_seen = time.time()
        return client, conv

    def begin_exchange(self, client_id: str, tag: str, exchange: Exchange) -> None:
        """Register an in-flight exchange so REST shows it before it completes."""
        client, conv = self.get_or_create_conversation(client_id, tag)
        conv.append(exchange)
        client.last_seen = time.time()
        self._inflight[exchange.id] = exchange
        self._inflight_cids[exchange.id] = conv.id

    def add_exchange(self, client_id: str, tag: str, exchange: Exchange) -> tuple[Client, Conversation]:
        client, conv = self.get_or_create_conversation(client_id, tag)
        pending = self._inflight.pop(exchange.id, None)
        if pending is not None:
            # Finalize the placeholder in place: its sequence (assigned at
            # begin_exchange) and position in the ring buffer stay stable.
            pending.client_request = exchange.client_request
            pending.server_response = exchange.server_response
            pending.timings = exchange.timings
            pending.usage = exchange.usage
            pending.error = exchange.error
            pending.in_flight = False
            self._inflight_cids.pop(exchange.id, None)
        else:
            conv.append(exchange)
        client.last_seen = time.time()
        return client, conv

    def list_clients(self) -> list[Client]:
        return list(self._clients.values())

    def all_conversations(self) -> list[Conversation]:
        return list(self._conversations.values())

    def get_conversation(self, cid: str) -> Conversation | None:
        return self._conversations.get(cid)

    def clear_conversation(self, cid: str) -> bool:
        for ex_id in [e for e, c in self._inflight_cids.items() if c == cid]:
            self._inflight.pop(ex_id, None)
            self._inflight_cids.pop(ex_id, None)
        return self._conversations.pop(cid, None) is not None

    def remove_client(self, client_id: str) -> bool:
        """Drop a client and all of its conversations. It re-registers on the next request."""
        if client_id not in self._clients:
            return False
        del self._clients[client_id]
        for cid in [cid for cid, cv in self._conversations.items() if cv.client_id == client_id]:
            self.clear_conversation(cid)
        return True
