# Build context is backend/ (see backend/local/docker-compose.yml) so this
# can COPY both the service and the shared package it path-depends on.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# Preserve the same relative layout as the repo (services/ingestion/../../shared)
# so the path dependency in pyproject.toml resolves identically in and out of Docker.
WORKDIR /repo/backend
COPY shared shared
COPY services/ingestion services/ingestion

WORKDIR /repo/backend/services/ingestion
RUN uv sync --frozen --no-dev

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD uv run python -c "import urllib.request as u; u.urlopen('http://localhost:8000/healthz', timeout=2)"

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
