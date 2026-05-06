#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/yc-radar}"
PYTHON_VERSION="${PYTHON_VERSION:-3.13}"
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-8}"
RESTART_PIPELINE="${RESTART_PIPELINE:-0}"
UV_BIN="${UV_BIN:-/home/ubuntu/.local/bin/uv}"

cd "$APP_DIR"

mkdir -p logs data/local/cache data/local/debug data/local/secrets

"$UV_BIN" sync --python "$PYTHON_VERSION" --extra dev

docker compose up -d redis flower

tmux kill-session -t yc-worker 2>/dev/null || true
tmux new-session -d -s yc-worker \
  "cd '$APP_DIR' && '$UV_BIN' run celery -A yc_radar.worker worker -Q classification --concurrency '$CELERY_CONCURRENCY' --loglevel INFO -E > logs/worker.log 2>&1"

if [[ "$RESTART_PIPELINE" == "1" ]]; then
  tmux kill-session -t yc-pipeline 2>/dev/null || true
  tmux new-session -d -s yc-pipeline \
    "cd '$APP_DIR' && '$UV_BIN' run python scripts/run_full_pipeline.py --wait-classification > logs/pipeline.log 2>&1"
fi

tmux ls
