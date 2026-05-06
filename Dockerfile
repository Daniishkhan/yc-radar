FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY data ./data
COPY extract_yc_companies.py ./extract_yc_companies.py

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "yc_radar.main:app", "--host", "0.0.0.0", "--port", "8000"]
