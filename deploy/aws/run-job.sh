#!/usr/bin/env bash
set -Eeuo pipefail

readonly WORKER_ENV=/etc/radar/worker.env
readonly RADAR_ROOT=/srv/radar
readonly APP_DIR=${RADAR_ROOT}/app
readonly RUNTIME_ENV=${RADAR_ROOT}/config/runtime.env
readonly JOB_DIR=${RADAR_ROOT}/state/jobs

name=${1:-}
if [[ ! ${name} =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]; then
  echo "A valid job name is required" >&2
  exit 2
fi
readonly SPEC_FILE=${JOB_DIR}/${name}.json
readonly STATE_FILE=${JOB_DIR}/${name}.state.json
readonly CONTAINER_NAME=radar-job-${name}

if [[ ${EUID} -ne 0 ]]; then
  echo "radar-run-job must run as root" >&2
  exit 1
fi
if [[ ! -f ${WORKER_ENV} || ! -f ${RUNTIME_ENV} || ! -f ${SPEC_FILE} ]]; then
  echo "Job ${name} is not fully configured" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${WORKER_ENV}"
: "${RADAR_STATE_BUCKET:?RADAR_STATE_BUCKET is required}"
: "${RADAR_AWS_REGION:?RADAR_AWS_REGION is required}"

exec 9>"/run/lock/radar-job-${name}.lock"
if ! flock -n 9; then
  echo "Job ${name} is already running" >&2
  exit 1
fi

mapfile -d '' -t job_command < <(python3 - "${SPEC_FILE}" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text())
argv = payload.get("argv")
if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
    raise SystemExit("Job spec argv must be a non-empty array of non-empty strings")
for value in argv:
    sys.stdout.buffer.write(value.encode() + b"\0")
PY
)
if [[ ${#job_command[@]} -eq 0 ]]; then
  echo "Job ${name} has an empty or invalid command" >&2
  exit 1
fi

write_state() {
  local state=$1 exit_code=${2:-}
  python3 - "${STATE_FILE}" "${SPEC_FILE}" "${state}" "${exit_code}" <<'PY'
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys

state_path = Path(sys.argv[1])
spec_path = Path(sys.argv[2])
state = sys.argv[3]
exit_code = sys.argv[4]
prior = json.loads(state_path.read_text()) if state_path.exists() else {}
attempt = int(prior.get("attempt", 0)) + (1 if state == "running" else 0)
payload = {
    "argv": json.loads(spec_path.read_text())["argv"],
    "attempt": attempt,
    "state": state,
    "updated_at": datetime.now(UTC).isoformat(),
}
if state == "running":
    payload["started_at"] = payload["updated_at"]
else:
    payload["started_at"] = prior.get("started_at")
    payload["finished_at"] = payload["updated_at"]
if exit_code:
    payload["exit_code"] = int(exit_code)
temporary = state_path.with_suffix(".tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
os.chmod(temporary, 0o600)
os.replace(temporary, state_path)
PY
  aws s3 cp "${STATE_FILE}" \
    "s3://${RADAR_STATE_BUCKET}/jobs/${name}/state.json" \
    --region "${RADAR_AWS_REGION}" \
    --only-show-errors || echo "Warning: could not mirror ${name} state to S3" >&2
}

child_pid=
# shellcheck disable=SC2329  # Invoked indirectly by the TERM/INT trap.
stop_child() {
  local signal_code=143
  trap - TERM INT
  if [[ -n ${child_pid} ]] && kill -0 "${child_pid}" 2>/dev/null; then
    docker stop --time 30 "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    kill -TERM "${child_pid}" 2>/dev/null || true
    wait "${child_pid}" 2>/dev/null || true
  fi
  write_state interrupted "${signal_code}"
  exit "${signal_code}"
}
trap stop_child TERM INT

# A host crash can leave the exact-name transient container behind. It is never
# shared with another job, so removing this one stale container is safe.
if docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
  docker stop --time 30 "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  docker rm --force "${CONTAINER_NAME}" >/dev/null 2>&1 || true
fi

write_state running
compose=(docker compose --project-directory "${APP_DIR}" --env-file "${RUNTIME_ENV}" -f "${APP_DIR}/compose.prod.yml")
set +e
"${compose[@]}" run --rm --name "${CONTAINER_NAME}" app "${job_command[@]}" &
child_pid=$!
wait "${child_pid}"
exit_code=$?
set -e
child_pid=

if [[ ${exit_code} -eq 0 ]]; then
  write_state completed 0
  # Completed jobs must not run again on the next boot. Failed/interrupted jobs
  # remain enabled so systemd can retry the identical resumable command.
  systemctl disable "radar-job@${name}.service" >/dev/null 2>&1 || true
else
  write_state failed "${exit_code}"
fi
exit "${exit_code}"
