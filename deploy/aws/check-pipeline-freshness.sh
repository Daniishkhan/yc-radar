#!/usr/bin/env bash
set -Eeuo pipefail

readonly WORKER_ENV=/etc/radar/worker.env
readonly RADAR_ROOT=/srv/radar
readonly APP_DIR=${RADAR_ROOT}/app
readonly RUNTIME_ENV=${RADAR_ROOT}/config/runtime.env
readonly WORKLOAD_LOCK=/run/lock/radar-workload.lock

if [[ ${EUID} -ne 0 ]]; then
  echo "radar-check-pipeline-freshness must run as root" >&2
  exit 1
fi
if [[ ! -f ${WORKER_ENV} || ! -f ${RUNTIME_ENV} || ! -d ${APP_DIR}/.git ]]; then
  echo "Worker is not bootstrapped; expected ${WORKER_ENV}, ${RUNTIME_ENV}, and ${APP_DIR}" >&2
  exit 1
fi

exec 9>"${WORKLOAD_LOCK}"
if ! flock -n 9; then
  echo "Another Radar workload is active; skipping the pipeline freshness check"
  exit 0
fi

compose=(
  docker compose
  --project-directory "${APP_DIR}"
  --env-file "${RUNTIME_ENV}"
  -f "${APP_DIR}/compose.prod.yml"
)
exec "${compose[@]}" run --rm app \
  python scripts/check_pipeline_freshness.py \
  --max-age-hours 24 \
  --output /app/data/local/runs/current/pipeline_freshness.json
