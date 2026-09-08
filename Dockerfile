# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UI_DIR=/app/ui

WORKDIR /app

# Install the package (and its dependencies); pyproject.toml is the single
# source of truth for requirements.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Static UI (served from UI_DIR).
COPY ui ./ui

# Non-root user.
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import sys,urllib.request;urllib.request.urlopen('http://127.0.0.1:9090/health')" || sys.exit(1)

CMD ["uvicorn", "llm_proxy.app:app", "--host", "0.0.0.0", "--port", "9090", "--workers", "1"]
