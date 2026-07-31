#!/usr/bin/env bash
set -Eeuo pipefail

readonly WORKER_ENV=/etc/radar/worker.env
readonly RADAR_ROOT=/srv/radar
readonly APP_DIR=${RADAR_ROOT}/app
readonly RUNTIME_ENV=${RADAR_ROOT}/config/runtime.env
readonly DEPLOY_STATE=${RADAR_ROOT}/state/deployment.json

fetch=true
case "${1:-}" in
  "") ;;
  --no-fetch) fetch=false ;;
  *) echo "Usage: radar-deploy [--no-fetch]" >&2; exit 2 ;;
esac

if [[ ${EUID} -ne 0 ]]; then
  echo "radar-deploy must run as root" >&2
  exit 1
fi
if [[ ! -f ${WORKER_ENV} || ! -f ${RUNTIME_ENV} || ! -d ${APP_DIR}/.git ]]; then
  echo "Worker is not bootstrapped; expected ${WORKER_ENV}, ${RUNTIME_ENV}, and ${APP_DIR}" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${WORKER_ENV}"
: "${RADAR_REPO_BRANCH:?RADAR_REPO_BRANCH is required}"
: "${RADAR_STATE_BUCKET:?RADAR_STATE_BUCKET is required}"
: "${RADAR_AWS_REGION:?RADAR_AWS_REGION is required}"

exec 9>/run/lock/radar-deploy.lock
if ! flock -n 9; then
  echo "Another deployment is already running" >&2
  exit 1
fi

active_jobs=$(systemctl list-units \
  --type=service \
  --state=activating,running \
  --no-legend \
  'radar-job@*.service' || true)
if [[ -n ${active_jobs} ]]; then
  echo "A managed Radar job is active; wait for it or stop it before deploying" >&2
  printf '%s\n' "${active_jobs}" >&2
  exit 1
fi

if ${fetch}; then
  if [[ -n $(git -C "${APP_DIR}" status --porcelain --untracked-files=no) ]]; then
    echo "Tracked changes exist in ${APP_DIR}; refusing to overwrite them" >&2
    exit 1
  fi
  git -C "${APP_DIR}" fetch --prune origin "${RADAR_REPO_BRANCH}"
  git -C "${APP_DIR}" checkout "${RADAR_REPO_BRANCH}"
  git -C "${APP_DIR}" merge --ff-only "origin/${RADAR_REPO_BRANCH}"
fi

install -m 0755 "${APP_DIR}/deploy/aws/deploy-host.sh" /usr/local/sbin/radar-deploy
install -m 0755 "${APP_DIR}/deploy/aws/run-job.sh" /usr/local/sbin/radar-run-job
install -m 0755 "${APP_DIR}/deploy/aws/jobctl.sh" /usr/local/sbin/radar-jobctl
install -m 0755 \
  "${APP_DIR}/deploy/aws/configure-gcp-wif.sh" \
  /usr/local/sbin/radar-configure-gcp-wif
install -m 0755 \
  "${APP_DIR}/deploy/aws/configure-tailscale-exit-node.sh" \
  /usr/local/sbin/radar-configure-tailscale-exit-node
install -m 0644 "${APP_DIR}/deploy/systemd/radar-deploy.service" /etc/systemd/system/radar-deploy.service
install -m 0644 "${APP_DIR}/deploy/systemd/radar-job@.service" /etc/systemd/system/radar-job@.service
/usr/local/sbin/radar-configure-gcp-wif
/usr/local/sbin/radar-configure-tailscale-exit-node
systemctl daemon-reload

install -d -m 0755 "${APP_DIR}/data/local" "${RADAR_ROOT}/state"
chown -R 1000:1000 "${APP_DIR}/data/local" "${APP_DIR}/data/snapshots"

compose=(docker compose --project-directory "${APP_DIR}" --env-file "${RUNTIME_ENV}" -f "${APP_DIR}/compose.prod.yml")
"${compose[@]}" build --pull app
"${compose[@]}" up -d postgres
"${compose[@]}" run --rm app alembic upgrade head

revision=$(git -C "${APP_DIR}" rev-parse HEAD)
python3 - "${DEPLOY_STATE}" "${revision}" <<'PY'
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
payload = {
    "deployed_at": datetime.now(UTC).isoformat(),
    "revision": sys.argv[2],
    "state": "completed",
}
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
os.chmod(temporary, 0o644)
os.replace(temporary, path)
PY

# S3 versioning preserves prior deployment summaries. The retained EBS copy is authoritative.
aws s3 cp "${DEPLOY_STATE}" \
  "s3://${RADAR_STATE_BUCKET}/deployment/current.json" \
  --region "${RADAR_AWS_REGION}" \
  --only-show-errors || echo "Warning: could not mirror deployment state to S3" >&2

echo "Deployed YC Radar revision ${revision}"
