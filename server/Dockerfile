# One image for the API, worker, scheduler and admin commands.
# Build from the repository root: docker build -f server/Dockerfile server
FROM ghcr.io/astral-sh/uv:0.7.17-python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
RUN uv sync --locked --no-dev

FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --home-dir /app gatherloom \
    && mkdir -p /data/storage /data/state \
    && chown -R gatherloom /data
WORKDIR /app
COPY --from=build --chown=gatherloom /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    GATHERLOOM_STORAGE_DIR=/data/storage \
    GATHERLOOM_DELETION_LOG=/data/state/deletions.jsonl
USER gatherloom
# jieba builds its dictionary cache on first use; do it at build time.
RUN python -c "import jieba; jieba.initialize()"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
