# UI build
FROM node:alpine AS ui

WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY ui ./ui
RUN npm run build

# Backend build
FROM python:3.14-alpine AS build

RUN apk add --no-cache --update git
RUN pip install --no-cache-dir build

ARG VERSION
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}

WORKDIR /app
COPY . .
COPY --from=ui /src/llm_proxy/web llm_proxy/web
RUN python -m build --wheel

# Runtime
FROM alpine:3.24

RUN apk add --no-cache --update python3 pipx tini

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ENV PIPX_HOME=/opt/pipx
ENV PIPX_BIN_DIR=/usr/bin

COPY --from=build /app/dist/llm_mitm_proxy-*.whl /tmp/
RUN pipx install /tmp/llm_mitm_proxy-*.whl && rm /tmp/llm_mitm_proxy-*.whl

RUN adduser -D -u 1000 -h /dev/null -s /sbin/nologin appuser
USER appuser

EXPOSE 8081 9090

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9090/health', timeout=8)"

ENTRYPOINT ["/sbin/tini", "--"]

CMD ["/bin/sh", "-c", "llm-mitm-proxy ${UPSTREAM_BASE_URL:+$UPSTREAM_BASE_URL} ${UPSTREAM_API_KEY:+--upstream-api-key $UPSTREAM_API_KEY} ${LISTEN_HOST:+--host $LISTEN_HOST} ${PROXY_PORT:+--proxy-port $PROXY_PORT} ${WEB_PORT:+--web-port $WEB_PORT} ${LOG_LEVEL:+--log-level $LOG_LEVEL}"]
