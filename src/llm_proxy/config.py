"""Environment-based configuration (pydantic-settings).

Every field maps to an env var of the same name (case-insensitive), e.g.
``UPSTREAM_BASE_URL`` -> ``upstream_base_url``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- listen ---
    listen_host: str = "0.0.0.0"
    listen_port: int = 9090

    # --- upstream (single local llama.cpp server) ---
    upstream_base_url: str = "http://host.docker.internal:8080"
    # Optional server-side fallback key; only injected when a client sends no key.
    upstream_api_key: str | None = None

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

    # --- misc ---
    # Override the static UI directory (defaults to the package-relative ``ui/``).
    ui_dir: str | None = None
    log_level: str = "info"
    use_uvloop: bool = False


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (cached; read once)."""
    return Settings()
