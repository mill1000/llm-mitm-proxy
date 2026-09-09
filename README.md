# LLM Proxy

A low-overhead, OpenAI-compatible reverse proxy with a live **"conversation" WebUI**
for inspecting, replaying, and exporting client ↔ server traffic to a **local
llama.cpp** upstream.

Think **mitmproxy + the llama.cpp WebUI**: clients on the left dock, the
conversation in the main pane (client request on the left, server response on the
right), live tokens, timestamps + deltas, and expandable full wire calls.

**See [PLAN.md](./PLAN.md)** for the full design, key decisions, and milestone breakdown.

## Status

**M4 — polish** (this tree). The proxy forwards `/v1/*` to the upstream
(pooled, streaming tap-and-forward), captures every exchange into an in-memory
ring buffer, and serves a live **conversation WebUI** over a single WebSocket
(`/ws`): client request on the left, server response on the right, tokens
rendered live, timestamps + timing deltas (TTFT / total / tok/s), and
expandable full wire calls. Any captured exchange can be **replayed** to the
upstream: the per-card **replay** button opens a **bottom-dock editor**
pre-populated with the captured request (quick fields for model/params plus a
raw JSON body, which is the source of truth); sending re-sends the edited body
in place of the captured one and the result is appended flagged with a `replay`
badge. Reasoning models (e.g. llama.cpp `--reasoning-preserve`) stream their
thinking live into a collapsible "thinking" block (expanded while streaming,
auto-collapsed on completion); tok/s comes from the generation timings
llama.cpp embeds in its responses when available. Exchanges and conversations
export as JSON (secrets redacted). M3's **adapter registry** (config-driven
in/out adapters; a bad name fails startup), **upstream timeouts**,
and **WebSocket liveness** (server pings, dead sockets pruned) are all in
place; the image is a two-stage non-root `alpine` build.

- M0 — foundation (proxy, capture, REST, export) — done
- M1 — live streaming UI (WebSocket fan-out) + conversation view — done
- M2 — replay + full JSON export — done
- M3 — adapter seams + hardening (timeouts, WS liveness, non-root multi-arch image) — done
- M4 — polish (replay editor dock, collapsible thinking, minimal compose) — done

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
The image runs as a non-root user and is arch-neutral; for a multi-arch image:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t llm-proxy:latest .
```

## Quick start (local, no Docker)

```bash
# Python 3.11–3.14 (3.14 preferred; that's what the Docker build image and
# devcontainer use)
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

llm-proxy http://localhost:8080
```

The upstream base URL is the positional argument (the package reads no
environment variables — see Configuration below).

Then call it like any OpenAI endpoint:

```bash
curl http://localhost:9090/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"local","stream":false,"messages":[{"role":"user","content":"hi"}]}'

curl http://localhost:9090/api/clients
curl http://localhost:9090/api/conversations/127.0.0.1/export

# Replay the first captured exchange as captured (an empty JSON body re-sends
# the captured request; a non-empty body replaces it entirely)
curl -X POST http://localhost:9090/api/conversations/127.0.0.1/exchanges/0/replay \
  -H 'content-type: application/json' -d '{}'
```

## Tests

Install the package, then run the end-to-end suite (M0 + M1 + M2 + M3; it spins up a
mock llama.cpp upstream on `127.0.0.1:8082`):

```bash
pip install -e ".[dev]"
python -m unittest discover -v
```

## Development (style)

Python is formatted with **isort + black** (configured in `pyproject.toml`):

```bash
pip install -e ".[dev]"
isort llm_proxy tests
black llm_proxy tests
```

JS is verified with **`node --check` + ESLint** (flat config in `eslint.config.mjs`;
the UI itself has no build step — plain JS served from `ui/`):

```bash
npm install
npm run verify:js
```

## Dev container

The repo includes a [devcontainer](./.devcontainer/devcontainer.json) (Python 3.14 +
Node LTS). In VS Code: **Dev Containers: Reopen in Container**. On startup it
installs the package (`.[dev]`) into the image's system Python (no
`.venv` is created) and runs `npm install`.

Inside the container:

```bash
python -m unittest discover -v      # Python test suite (M0 + M1 + M2 + M3)
npm run verify:js                   # node --check + ESLint over ui/
llm-proxy http://localhost:8080
```

The WebUI needs a real browser: open the auto-forwarded `http://localhost:9090`
and point a client at `http://localhost:9090/v1/chat/completions` to see the
conversation view live.

## Versioning

The package version is derived from **git tags** at install/build time
(`setuptools_scm`). Tag CalVer-style releases, e.g. `git tag 2026.09.08`;
untagged checkouts report a `dev` version with the commit id.

- **Local / devcontainer**: `pip install -e .` bakes the current tag into the
  install; re-run it after creating a tag to refresh.
- **Docker**: the build stage compiles the wheel from the full context (which
  includes `.git`), so the current tag is picked up automatically;
  `docker build --build-arg VERSION=2026.09.08 .` overrides it. The runtime
  stage is a minimal `alpine` image (wheel installed via `pipx`, non-root).

## Configuration

The package reads **no environment variables** — configuration is explicit.
`llm-proxy` takes the command-line options below (anything omitted falls back
to the defaults); the Docker image's `CMD` maps container env vars onto those
options, so env vars remain a container-layer convenience only.

### Command line

```
usage: llm-proxy [-h] [--version] [--host HOST] [--port PORT]
                 [--upstream-api-key KEY] [--log-level LEVEL] [--ui-dir DIR]
                 [UPSTREAM_BASE_URL]
```

| Arg | Notes |
|---|---|
| `UPSTREAM_BASE_URL` (positional) | upstream base URL (default `http://host.docker.internal:8080`) |
| `--host` | listen host (default `0.0.0.0`) |
| `--port` | listen port (default `9090`, kept off llama.cpp's `8080`) |
| `--upstream-api-key` | optional server-side fallback key, injected only when a client sends no key |
| `--log-level` | app loggers, incl. the `llm_proxy.ws` connection trace (`debug` = per-event fan-out) |
| `--ui-dir` | static UI directory (default: the package-relative `ui/`; the image passes `/app/ui`) |
| `--help` / `--version` | usage / package version |

### Docker env vars

The image `CMD` maps these container env vars onto the CLI (unset or empty
values are skipped, so the defaults apply):

| Var | CLI arg |
|---|---|
| `UPSTREAM_BASE_URL` | positional upstream |
| `UPSTREAM_API_KEY` | `--upstream-api-key` |
| `LISTEN_HOST` | `--host` |
| `LISTEN_PORT` | `--port` |
| `LOG_LEVEL` | `--log-level` |

### Defaults reference

Settings not exposed on the command line are always their defaults — a
deliberate tradeoff that keeps the CLI surface minimal; add an option if one of
these needs to be tunable:

| Setting | Default | Notes |
|---|---|---|
| `in_adapter` / `out_adapter` | `openai` / `openai` | Adapter registry names; a bad name fails startup (cross-format is M5) |
| `upstream_connect_timeout` | `10` | Seconds to establish an upstream connection |
| `upstream_read_timeout` | `300` | Max gap between upstream bytes (covers slow generation) |
| `upstream_pool_timeout` | `30` | Wait for a free pooled connection |
| `client_id_header` | *(unset)* | Explicit-identity fallback when the real IP is hidden |
| `split_conversations` | *(unset)* | `history` = no-key auto-split (off by default) |
| `retention_max_exchanges` | `500` | Per-client ring buffer cap |
| `retention_max_age_hours` | `24` | Age cap for captured exchanges |
| `include_raw_chunks` | `false` | Keep raw SSE chunks in the store (memory-heavy) |
| `ws_ping_interval` / `ws_ping_timeout` | `20` / `20` | Liveness: the hub pings each UI socket every interval and closes it if no pong arrives within interval + timeout. The WebUI answers automatically; other WS clients that don't will be pruned after silence |

## Layout

```
llm_proxy/
  app.py               # app factory + llm-proxy console entry point (proxy, UI REST, /ws, static UI)
  config.py            # settings model (defaults + explicit overrides; no env)
  hub.py               # WebSocket fan-out hub (non-blocking live UI delivery + liveness)
  logconf.py           # logging setup (text logs)
  proxy/               # /v1/* router, pipeline (tap-and-forward), SSE parser
  adapters/            # in/out adapter protocols + registry + openai adapter
  model/               # normalized IR + conversation objects (ring buffer)
  store/               # in-memory store + retention
  api/                 # /api/* UI endpoints
  dump.py              # JSON export
ui/                    # static single-page UI (index.html, app.js, styles.css; no build)
Dockerfile, docker-compose.yml
```
