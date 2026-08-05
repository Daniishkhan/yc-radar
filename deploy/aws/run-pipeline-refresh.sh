#!/usr/bin/env bash
set -Eeuo pipefail

readonly WORKER_ENV=/etc/radar/worker.env
readonly RADAR_ROOT=/srv/radar
readonly APP_DIR=${RADAR_ROOT}/app
readonly RUNTIME_ENV=${RADAR_ROOT}/config/runtime.env
readonly CONTAINER_RUN_DIR=/app/data/local/runs/current

if [[ ${EUID} -ne 0 ]]; then
  echo "radar-run-pipeline-refresh must run as root" >&2
  exit 1
fi
if [[ ! -f ${WORKER_ENV} || ! -f ${RUNTIME_ENV} || ! -d ${APP_DIR}/.git ]]; then
  echo "Worker is not bootstrapped; expected ${WORKER_ENV}, ${RUNTIME_ENV}, and ${APP_DIR}" >&2
  exit 1
fi

exec 9>/run/lock/radar-pipeline-refresh.lock
if ! flock -n 9; then
  echo "Another recurring pipeline refresh is already running" >&2
  exit 1
fi

compose=(
  docker compose
  --project-directory "${APP_DIR}"
  --env-file "${RUNTIME_ENV}"
  -f "${APP_DIR}/compose.prod.yml"
)
exit_code=0

run_stage() {
  local stage=$1
  shift
  echo "Starting recurring pipeline stage: ${stage}"
  if "${compose[@]}" run --rm app "$@"; then
    echo "Completed recurring pipeline stage: ${stage}"
  else
    local stage_exit=$?
    echo "Recurring pipeline stage ${stage} failed with exit ${stage_exit}" >&2
    exit_code=1
  fi
}

# Source adapters use only documented public endpoints. The synchronizer is sequential
# and applies lifecycle changes only from complete snapshots.
run_stage source-sync \
  python scripts/sync_job_sources.py \
  --delay-seconds 2

# Queue generation is deterministic and does not perform applications or paid enrichment.
run_stage application-and-verification-queues \
  python scripts/generate_job_opportunities.py \
  --output-dir "${CONTAINER_RUN_DIR}" \
  --limit 200000 \
  --queue-limit 500

run_stage company-outreach-queue \
  python scripts/generate_weekly_targets.py \
  --no-verify-hiring \
  --no-llm \
  --output-dir "${CONTAINER_RUN_DIR}"

# Validate only job queues. Company outreach is intentionally not treated as an
# application queue and may not have a direct job URL.
run_stage application-url-validation \
  python scripts/validate_application_urls.py \
  --queue "application_queue=${CONTAINER_RUN_DIR}/application_queue.json" \
  --queue "verification_queue=${CONTAINER_RUN_DIR}/verification_queue.json" \
  --output "${CONTAINER_RUN_DIR}/application_url_validations.json" \
  --delay-seconds 1

run_stage application-pool-metrics \
  python scripts/report_application_pool.py \
  --run-dir "${CONTAINER_RUN_DIR}" \
  --url-validations "${CONTAINER_RUN_DIR}/application_url_validations.json" \
  --output "${CONTAINER_RUN_DIR}/application_pool_metrics.json"

exit "${exit_code}"
