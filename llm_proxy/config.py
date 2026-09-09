"""Application settings: defaults + explicit overrides.

The package reads **no environment variables**. Configuration is explicit:
``llm-proxy`` passes command-line values in, and the Docker image's ``CMD``
maps container env vars onto those CLI arguments (env vars are a Docker
concern only).
"""

from __future__ import annotations

from pydantic import BaseModel


class Settings(BaseModel):
    # --- listen ---
    listen_host: str = "0.0.0.0"
    listen_port: int = 9090

    # --- upstream (single local llama.cpp server) ---
    upstream_base_url: str = "http://host.docker.internal:8080"
    # Optional server-side fallback key; only injected when a client sends no key.
    upstream_api_key: str | None = None
    # Upstream timeouts (seconds). read = max gap between upstream bytes (covers
    # slow LLM generation; a dead link trips it far sooner than the default).
    upstream_connect_timeout: float = 10.0
    upstream_read_timeout: float = 300.0
    upstream_pool_timeout: float = 30.0

    # --- adapters (independent; cross-format is roadmap) ---
    in_adapter: str = "openai"
    out_adapter: str = "openai"

    # --- client identity ---
    # Explicit-identity fallback header (used when the real source IP is not visible).
    client_id_header: str | None = None

    # --- conversation splitting (opt-in; "history" enables message-history boundary detection) ---
    split_conversations: str | None = None

    # --- retention (per-client ring buffer) ---
    retention_max_exchanges: int = 500
    retention_max_age_hours: int = 24

    # --- capture ---
    # Keep raw SSE chunks in the store (memory-heavy; off by default).
    include_raw_chunks: bool = False

    # --- live UI (WebSocket) ---
    # App-level liveness: the hub pings each UI socket every ws_ping_interval and
    # closes it if no pong arrives within ws_ping_timeout. This prunes half-open
    # sockets (network drop without FIN) that would otherwise linger in the hub.
    ws_ping_interval: float = 20.0
    ws_ping_timeout: float = 20.0

    # --- misc ---
    # Override the static UI directory (defaults to the package-relative ``ui/``).
    ui_dir: str | None = None
    log_level: str = "info"
    use_uvloop: bool = False
