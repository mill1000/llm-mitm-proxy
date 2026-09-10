# LLM Proxy

A low-overhead, **transparent MITM proxy for LLM APIs** with a live
**"conversation" WebUI** for inspecting, replaying, and exporting client ↔
server traffic to a **local llama.cpp** upstream.

Think **mitmproxy + the llama.cpp WebUI**: clients on the left dock, the
conversation in the main pane (client request on the left, server response on the
right), live tokens, timestamps + deltas, and expandable full wire calls.
Every request (any method, any path) is forwarded to the upstream verbatim;
each request is decoded *per request* — chat completions get the full
conversation view, everything else is captured opaquely (raw, capped) — so
nothing ever 404s at the proxy.

**See [PLAN.md](./PLAN.md)** for the full design, key decisions, and milestone breakdown.

## Status

**M8 — minimal frontend build** (this tree; awaiting commit). The proxy stays a
transparent catch-all: one process, two listeners — the **LLM port (default
8080)** forwards every method/path (query string included) to the upstream
verbatim with **no reserved routes**, so clients like Zed work through it with
zero 404s; the **UI port (default 9090)** serves the WebUI, `/api/*`, `/ws`,
and `/health` only. Decoding is **per request** (observation-only, never
rewrites the wire): `POST */chat/completions` is decoded by the `openai`
dissector into the full conversation view (live tokens, timestamps + timing
deltas, expandable wire calls, tool calls, **replay** with a bottom-dock
editor, collapsible thinking block, JSON export with secrets redacted); every
other request (`/v1/models`, `/props`, unknown paths, …) falls back to the
`generic` dissector — opaque raw capture (capped), logged at `debug`.

- M0 — foundation (proxy, capture, REST, export) — done
- M1 — live streaming UI (WebSocket fan-out) + conversation view — done
- M2 — replay + full JSON export — done
- M3 — hardening (timeouts, WS liveness, non-root multi-arch image) — done
- M4 — polish (replay editor dock, collapsible thinking, minimal compose) — done
- M5 — transparent MITM core (two-port: LLM catch-all 8080, UI 9090) — done
- M6 — per-request decode (openai chat decoder + generic fallback, tool-calls UI) — done
- M7 — test reorganization (by subject: cli, proxy, ws, ui-api, dissectors, shutdown) — done
- M8 — minimal frontend build (esbuild bundle + minify into the package; wheel ships the UI) — done (awaiting commit)

## Quick start (Docker)

```bash
# Build and run (expects llama.cpp reachable at host.docker.internal:8080)
docker compose up -d --build
```

- **LLM API proxy** (point clients here): `http://localhost:8080` — e.g. Zed's
  base URL is `http://localhost:8080/v1`.
- **Web UI** (plus the proxy's own `/api/*`, `/ws`, `/health`):
  `http://localhost:9090/`.

Point the proxy at your llama.cpp server by setting `UPSTREAM_BASE_URL` in
`docker-compose.yml` (default is `http://host.docker.internal:8080`). If your
upstream requires a key, set `UPSTREAM_API_KEY` — it is only injected when a
client sends no key; client-supplied keys are always passed through unchanged.
The image runs as a non-root user and is
arch-neutral; for a multi-arch image:

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
environment variables — see Configuration below). Both listeners start: the LLM
proxy on `8080` and the WebUI/API on `9090`. (An ad-hoc `uvicorn
llm_proxy.app:app` run starts the **UI listener only** with default settings.)
Chat completions are decoded into the full conversation view; every other
request is captured opaquely.

Then call it like any OpenAI endpoint — clients hit the LLM port, the proxy's
own API stays on the UI port:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"local","stream":false,"messages":[{"role":"user","content":"hi"}]}'

curl http://localhost:8080/props?model=local   # any path is forwarded as-is
curl http://localhost:9090/api/clients
curl http://localhost:9090/api/conversations/127.0.0.1/export

# Replay the first captured exchange as captured (an empty JSON body re-sends
# the captured request; a non-empty body replaces it entirely)
curl -X POST http://localhost:9090/api/conversations/127.0.0.1/exchanges/0/replay \
  -H 'content-type: application/json' -d '{}'
```

## Tests

Install the package, then run the end-to-end suite (M0–M6; it spins up a
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

JS is **built with esbuild** (`ui/` sources → minified bundle inside the package
at `llm_proxy/web/`, with `marked` bundled in from npm) and verified with
**`node --check` + ESLint** (flat config in `eslint.config.mjs`). The build is
one-time per change — the server serves the static in-package output
(zero per-request cost):

```bash
npm install
npm run build          # one-time; use `npm run watch` while iterating on the UI
npm run verify:js      # node --check + ESLint over ui/
```

Running the proxy without a built UI still works for the LLM port; the Web UI
mount is skipped with a warning until `npm run build` has been run.

## Dev container

The repo includes a [devcontainer](./.devcontainer/devcontainer.json) (Python 3.14 +
Node LTS). In VS Code: **Dev Containers: Reopen in Container**. On startup it
installs the package (`.[dev]`) into the image's system Python (no
`.venv` is created), runs `npm install`, and builds the UI (`npm run build`).

Inside the container:

```bash
python -m unittest discover -v      # Python test suite (M0–M8)
npm run build && npm run verify:js  # esbuild bundle + node --check + ESLint
npm run watch                       # optional: rebuild the UI on save
llm-proxy http://localhost:8080
```

The WebUI needs a real browser: open the auto-forwarded `http://localhost:9090`
and point a client at `http://localhost:8080/v1/chat/completions` to see the
conversation view live (both ports are auto-forwarded).

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
- **Release (PyPI)**: the wheel is the full app — build the UI *before*
  building the wheel so `llm_proxy/web/` is included:
  `npm run build && python -m build`. A wheel built without the UI still works
  (LLM port) but skips the Web UI with a startup warning.

## Configuration

The package reads **no environment variables** — configuration is explicit.
`llm-proxy` takes the command-line options below (anything omitted falls back
to the defaults); the Docker image's `CMD` maps container env vars onto those
options, so env vars remain a container-layer convenience only.

### Command line

```
usage: llm-proxy [-h] [--version] [--host HOST] [--llm-port PORT] [--ui-port PORT]
                 [--upstream-api-key KEY] [--log-level LEVEL] [--ui-dir DIR]
                 [UPSTREAM_BASE_URL]
```

| Arg | Notes |
|---|---|
| `UPSTREAM_BASE_URL` (positional) | upstream base URL (default `http://host.docker.internal:8080`) |
| `--host` | listen host for both listeners (default `0.0.0.0`) |
| `--llm-port` | LLM proxy listener port (default `8080`, the transparent catch-all) |
| `--ui-port` | WebUI + `/api/*` + `/ws` + `/health` listener port (default `9090`) |
| `--upstream-api-key` | optional server-side fallback key, injected only when a client sends no key |
| `--log-level` | app loggers, incl. the `llm_proxy.ws` connection trace at `debug` (connect/focus/disconnect) |
| `--ui-dir` | static UI directory (default: the in-package `llm_proxy/web` build) |
| `--help` / `--version` | usage / package version |

### Docker env vars

The image `CMD` maps these container env vars onto the CLI (unset or empty
values are skipped, so the defaults apply):

| Var | CLI arg |
|---|---|
| `UPSTREAM_BASE_URL` | positional upstream |
| `UPSTREAM_API_KEY` | `--upstream-api-key` |
| `LISTEN_HOST` | `--host` |
| `LLM_PORT` | `--llm-port` |
| `UI_PORT` | `--ui-port` |
| `LOG_LEVEL` | `--log-level` |

### WebUI

Browser-side WS tracing (dev-console `[ws] …` lines) is off by default. Enable
it per-session by opening the WebUI with the `wslog` query parameter, e.g.
`http://localhost:9090/?wslog` — no server setting or rebuild involved.

### Defaults reference

Settings not exposed on the command line are always their defaults — a
deliberate tradeoff that keeps the CLI surface minimal; add an option if one of
these needs to be tunable:

| Setting | Default | Notes |
|---|---|---|
| `upstream_connect_timeout` | `10` | Seconds to establish an upstream connection (the dead-link detector; there is deliberately no read timeout - as a transparent proxy, timeout policy belongs to the client) |
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
  app.py               # app factory: two listeners (LLM catch-all + UI) + llm-proxy entry point
  config.py            # settings model (defaults + explicit overrides; no env)
  hub.py               # WebSocket fan-out hub (non-blocking live UI delivery + liveness)
  logconf.py           # logging setup (text logs)
  proxy/               # catch-all LLM router, pipeline (tap-and-forward), SSE parser
  dissectors/          # observation dissectors: generic (fallback) + openai (chat decode); per-request selection
  model/               # wire/parse objects + conversation objects (ring buffer)
  store/               # in-memory store + retention
  api/                 # /api/* UI endpoints
  dump.py              # JSON export
ui/                    # single-page UI sources (index.html, app.js, styles.css)
llm_proxy/web/         # gitignored esbuild output (bundled marked, minified); shipped
                       #   in the wheel as package data and served from there
package.json           # esbuild build + JS verification (marked, eslint)
Dockerfile, docker-compose.yml
```
