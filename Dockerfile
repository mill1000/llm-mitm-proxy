# syntax=docker/dockerfile:1
# Build step: compile the wheel on a full Python. git is required by
# setuptools_scm (it reads the version from the git tags in the build context).
FROM python:3.14-alpine AS build
RUN apk add --no-cache --update git
RUN pip install --no-cache-dir build
# Optional version override; without it the version comes from git tags
# (see README "Versioning").
ARG VERSION
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
WORKDIR /app
COPY . .
RUN python -m build --wheel

# Runtime step: minimal alpine, the wheel installed via pipx.
FROM alpine:3.24
RUN apk add --no-cache --update python3 pipx tini
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # PIPX_HOME lives outside /root (mode 700) so the non-root user can use it.
    PIPX_HOME=/opt/pipx \
    PIPX_BIN_DIR=/usr/bin \
    UI_DIR=/app/ui
COPY --from=build /app/dist/llm_proxy-*.whl /tmp/
# The package ships the `llm-proxy` console script, which starts uvicorn.
RUN pipx install /tmp/llm_proxy-*.whl && rm /tmp/llm_proxy-*.whl

# Static UI (served from UI_DIR).
COPY ui /app/ui

# Run as non-root.
RUN adduser -D -u 1000 -h /dev/null -s /sbin/nologin appuser
USER appuser

EXPOSE 9090

# The /health endpoint probes the upstream itself (3s timeout), so give the
# healthcheck more headroom than that; the urlopen timeout keeps the whole
# probe inside the docker timeout. python3 exits nonzero on any failure.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9090/health', timeout=8)"

# tini is PID 1: it reaps orphaned children and forwards signals to uvicorn
# (uvicorn itself handles SIGTERM for a graceful shutdown).
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/usr/bin/llm-proxy"]
