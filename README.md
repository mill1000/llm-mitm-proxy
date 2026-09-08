# LLM Proxy

A low-overhead, OpenAI-compatible reverse proxy with a live **"conversation" WebUI**
for inspecting, replaying, and exporting client ↔ server traffic to a **local
llama.cpp** upstream.

Think **mitmproxy + the llama.cpp WebUI**: clients on the left dock, the
conversation in the main pane (client request on the left, server response on the
right), live tokens, timestamps + deltas, and expandable full wire calls.

**See [PLAN.md](./PLAN.md)** for the full design, key decisions, and milestone breakdown.

## Status

**M2 — replay + export** (this tree). The proxy forwards `/v1/*` to the
upstream (pooled, streaming tap-and-forward), captures every exchange into an
in-memory ring buffer, and serves a live **conversation WebUI** over a single
WebSocket (`/ws`): client request on the left, server response on the right,
tokens rendered live, timestamps + timing deltas (TTFT / total / tok/s), and
expandable full wire calls. Any captured exchange can be **replayed** to the
upstream (per-card button; re-sends the captured client request and is flagged
with a `replay` badge), and can be **exported** as JSON (single exchange or the
whole conversation; secrets redacted). Reasoning models (e.g. llama.cpp
`--reasoning-preserve`) stream their thinking live into a muted "thinking"
block; tok/s comes from the generation timings llama.cpp embeds in its
responses when available.

- M0 — foundation (proxy, capture, REST, export) — done
- M1 — live streaming UI (WebSocket fan-out) + conversation view — done
- M2 — replay + full JSON export — done
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

# Replay the first captured exchange as captured (JSON body = optional
# request-body overrides, e.g. {"model":"other-model"})
curl -X POST http://localhost:9090/api/conversations/127.0.0.1/exchanges/0/replay \
  -H 'content-type: application/json' -d '{}'
```

## Tests

Install the package, then run the end-to-end suite (M0 + M1 + M2; it spins up a
mock llama.cpp upstream on `127.0.0.1:8082`):

```bash
pip install -e ".[dev]"
python -m unittest discover -v
```

## Development (style)

Python is formatted with **isort + black** (configured in `pyproject.toml`):

```bash
pip install -e ".[dev]"
isort src tests
black src tests
```

JS is verified with **`node --check` + ESLint** (flat config in `eslint.config.mjs`;
the UI itself has no build step — plain JS served from `ui/`):

```bash
npm install
npm run verify:js
```

## Dev container

The repo includes a [devcontainer](./.devcontainer/devcontainer.json) (Python 3.12 +
Node LTS). In VS Code: **Dev Containers: Reopen in Container**. On startup it
installs the package (`.[dev]`) into the image's system Python (no
`.venv` is created) and runs `npm install`.

Inside the container:

```bash
python -m unittest discover -v      # Python test suite (M0 + M1 + M2)
npm run verify:js                   # node --check + ESLint over ui/
UPSTREAM_BASE_URL=http://localhost:8080 python -m uvicorn llm_proxy.app:app --port 9090
```

The WebUI needs a real browser: open the auto-forwarded `http://localhost:9090`
and point a client at `http://localhost:9090/v1/chat/completions` to see the
conversation view live.

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
| `LOG_LEVEL` | `info` | App loggers, incl. the `llm_proxy.ws` connection trace (`debug` = per-event fan-out) |

## Layout

```
src/llm_proxy/
  app.py               # FastAPI app factory (proxy + UI REST + /ws + static UI)
  config.py            # env-based settings
  hub.py               # WebSocket fan-out hub (non-blocking live UI delivery)
  proxy/               # /v1/* router, pipeline (tap-and-forward), SSE parser
  adapters/            # in/out adapter protocols + registry + openai adapter
  model/               # normalized IR + conversation objects (ring buffer)
  store/               # in-memory store + retention
  api/                 # /api/* UI endpoints
  dump.py              # JSON export
ui/                    # static single-page UI (index.html, app.js, styles.css; no build)
Dockerfile, docker-compose.yml
```
