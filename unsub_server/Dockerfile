# Build from the REPO ROOT so the unsub_server package dir is in context:
#   docker build -f unsub_server/Dockerfile -t cadence-unsub .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /srv
COPY unsub_server/pyproject.toml unsub_server/uv.lock unsub_server/
RUN cd unsub_server && uv sync --frozen --no-dev
COPY unsub_server unsub_server

EXPOSE 8080
# UNSUB_DATABASE_URL and CADENCE_UNSUB_SIGNING_KEY are provided at runtime.
CMD ["uv", "run", "--project", "unsub_server", "--frozen", "--no-dev", "uvicorn", "unsub_server.main:app", "--host", "0.0.0.0", "--port", "8080"]
