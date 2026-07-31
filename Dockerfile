FROM ghcr.io/astral-sh/uv:0.8.15 AS uv

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY data/snapshots ./data/snapshots

RUN mkdir -p /app/data/local \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin yc-radar \
    && chown -R yc-radar:yc-radar /app

USER yc-radar

CMD ["python", "-m", "yc_radar.cli", "--help"]
