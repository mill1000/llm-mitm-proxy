# LLM Proxy

A low-overhead, OpenAI-compatible reverse proxy with a live **"conversation" WebUI**
for inspecting, replaying, and exporting client ↔ server traffic to a **local
llama.cpp** upstream.

Think **mitmproxy + the llama.cpp WebUI**: clients on the left dock, the
conversation in the main pane (client request on the left, server response on the
right), live tokens, timestamps + deltas, and expandable full wire calls.

**See [PLAN.md](./PLAN.md)** for the full design, key decisions, and milestone breakdown.

## Status

**M0 — foundation** (this tree). The proxy forwards `/v1/*` to the upstream
(pooled, streaming tap-and-forward), captures every exchange into an in-memory
ring buffer, and exposes a small REST API + JSON export.

- M1 — live streaming UI (WebSocket) + conversation view
- M2 — replay + full JSON export
- M3 — adapter registry / cross-format seams + hardening

## Quick start (Docker)

```bash
# Build and run (expects llama.cpp reachable at host.docker.internal:8080)
docker compose up -d --build

# Web UI
open http://localhost:9090/
```

Point the proxy at your llama.cpp server by setting `UPSTREAM_BASE_URL` in
`docker-compose.yml` (default is `http://host.docker.internal:8080`). If your
upstream requires a key, set `UPSTREAM_API_KEY` — it is only injected when a
client sends no key; client-supplied keys are always passed through unchanged.

## Quick start (local, no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

UPSTREAM_BASE_URL=http://localhost:8080 python -m uvicorn llm_proxy.app:app --port 9090
```

Then call it like any OpenAI endpoint:

```bash
curl http://localhost:9090/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"local","stream":false,"messages":[{"role":"user","content":"hi"}]}'

curl http://localhost:9090/api/clients
curl http://localhost:9090/api/conversations/127.0.0.1/export
```

## Tests

Install the package, then run the M0 end-to-end suite (it spins up a mock
llama.cpp upstream on `127.0.0.1:8082`):

```bash
pip install -e ".[dev]"
python -m unittest discover -v
```

## Development (style)

Formatting is enforced with **isort + black** (configured in `pyproject.toml`):

```bash
pip install -e ".[dev]"
isort src tests
black src tests
```

## Configuration (env vars)

| Var | Default | Notes |
|---|---|---|
| `LISTEN_HOST` / `LISTEN_PORT` | `0.0.0.0` / `9090` | Kept off `8080` (llama.cpp's default) |
| `UPSTREAM_BASE_URL` | `http://host.docker.internal:8080` | Single local llama.cpp server |
| `UPSTREAM_API_KEY` | *(unset)* | Optional server-side fallback key |
| `IN_ADAPTER` / `OUT_ADAPTER` | `openai` / `openai` | Cross-format is roadmap (M4) |
| `CLIENT_ID_HEADER` | *(unset)* | Explicit-identity fallback when real IP is hidden |
| `SPLIT_CONVERSATIONS` | *(unset)* | `history` = no-key auto-split (off by default) |
| `RETENTION_MAX_EXCHANGES` | `500` | Per-client ring buffer cap |
| `RETENTION_MAX_AGE_HOURS` | `24` | Age cap for captured exchanges |
| `INCLUDE_RAW_CHUNKS` | `false` | Keep raw SSE chunks in the store (memory-heavy) |
| `UI_DIR` | *(package-relative `ui/`)* | Override the static UI directory (set in the Docker image) |
| `LOG_LEVEL` | `info` | |

## Layout

```
src/llm_proxy/
  app.py               # FastAPI app factory (proxy + UI REST + static UI, one process)
  config.py            # env-based settings
  proxy/               # /v1/* router, pipeline (tap-and-forward), SSE parser
  adapters/            # in/out adapter protocols + registry + openai adapter
  model/               # normalized IR + conversation objects (ring buffer)
  store/               # in-memory store + retention
  api/                 # /api/* UI endpoints
  dump.py              # JSON export
ui/                    # static single-page UI (no build step)
Dockerfile, docker-compose.yml
```
