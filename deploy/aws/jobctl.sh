#!/usr/bin/env bash
set -Eeuo pipefail

readonly WORKER_ENV=/etc/radar/worker.env
readonly JOB_DIR=/srv/radar/state/jobs

usage() {
  cat >&2 <<'USAGE'
Usage:
  radar-jobctl run NAME -- COMMAND [ARG ...]
  radar-jobctl create NAME -- COMMAND [ARG ...]
  radar-jobctl create-b64 NAME BASE64_JSON_ARGV
  radar-jobctl start NAME
  radar-jobctl retry NAME
  radar-jobctl stop NAME
  radar-jobctl status NAME
  radar-jobctl logs NAME [LINES]
  radar-jobctl list

Commands run inside the production app container. Use explicit checkpoint/status
paths under /app/data/local so a restarted unit resumes the same durable state.
USAGE
  exit 2
}

if [[ ${EUID} -ne 0 ]]; then
  echo "radar-jobctl must run as root" >&2
  exit 1
fi
if [[ ! -f ${WORKER_ENV} ]]; then
  echo "Worker is not bootstrapped: ${WORKER_ENV} is missing" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "${WORKER_ENV}"
: "${RADAR_STATE_BUCKET:?RADAR_STATE_BUCKET is required}"
: "${RADAR_AWS_REGION:?RADAR_AWS_REGION is required}"
install -d -m 0755 "${JOB_DIR}"

validate_name() {
  local name=$1
  if [[ ! ${name} =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]; then
    echo "Invalid job name '${name}'; use 1-64 letters, digits, dots, underscores, or hyphens" >&2
    exit 2
  fi
}

write_spec() {
  local name=$1
  shift
  if [[ ${1:-} != -- ]]; then
    usage
  fi
  shift
  if [[ $# -eq 0 ]]; then
    echo "A container command is required" >&2
    exit 2
  fi
  if systemctl is-active --quiet "radar-job@${name}.service"; then
    echo "Job ${name} is active; stop it before replacing its command" >&2
    exit 1
  fi

  python3 - "${JOB_DIR}/${name}.json" "$@" <<'PY'
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
payload = {"argv": sys.argv[2:], "schema_version": 1}
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
  mirror_spec "${name}"
}

write_spec_b64() {
  local name=$1 encoded=$2
  if systemctl is-active --quiet "radar-job@${name}.service"; then
    echo "Job ${name} is active; stop it before replacing its command" >&2
    exit 1
  fi
  python3 - "${JOB_DIR}/${name}.json" "${encoded}" <<'PY'
import base64
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
try:
    argv = json.loads(base64.b64decode(sys.argv[2], validate=True))
except Exception as exc:
    raise SystemExit(f"Invalid base64 JSON command: {exc}") from exc
if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
    raise SystemExit("Decoded command must be a non-empty JSON array of non-empty strings")
payload = {"argv": argv, "schema_version": 1}
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
  mirror_spec "${name}"
}

mirror_spec() {
  local name=$1
  aws s3 cp "${JOB_DIR}/${name}.json" \
    "s3://${RADAR_STATE_BUCKET}/jobs/${name}/spec.json" \
    --region "${RADAR_AWS_REGION}" \
    --only-show-errors || echo "Warning: could not mirror ${name} spec to S3" >&2
}

require_spec() {
  local name=$1
  if [[ ! -f ${JOB_DIR}/${name}.json ]]; then
    echo "No job specification exists for ${name}" >&2
    exit 1
  fi
}

start_job() {
  local name=$1
  require_spec "${name}"
  systemctl reset-failed "radar-job@${name}.service" 2>/dev/null || true
  systemctl enable --now "radar-job@${name}.service"
}

action=${1:-}
case "${action}" in
  run)
    [[ $# -ge 4 ]] || usage
    name=$2
    validate_name "${name}"
    shift 2
    write_spec "${name}" "$@"
    start_job "${name}"
    ;;
  create)
    [[ $# -ge 4 ]] || usage
    name=$2
    validate_name "${name}"
    shift 2
    write_spec "${name}" "$@"
    ;;
  create-b64)
    [[ $# -eq 3 ]] || usage
    name=$2
    validate_name "${name}"
    write_spec_b64 "${name}" "$3"
    ;;
  start)
    [[ $# -eq 2 ]] || usage
    name=$2
    validate_name "${name}"
    start_job "${name}"
    ;;
  retry)
    [[ $# -eq 2 ]] || usage
    name=$2
    validate_name "${name}"
    require_spec "${name}"
    systemctl reset-failed "radar-job@${name}.service" 2>/dev/null || true
    systemctl enable "radar-job@${name}.service"
    systemctl restart "radar-job@${name}.service"
    ;;
  stop)
    [[ $# -eq 2 ]] || usage
    name=$2
    validate_name "${name}"
    systemctl disable --now "radar-job@${name}.service"
    ;;
  status)
    [[ $# -eq 2 ]] || usage
    name=$2
    validate_name "${name}"
    systemctl status --no-pager --full "radar-job@${name}.service" || true
    if [[ -f ${JOB_DIR}/${name}.state.json ]]; then
      jq . "${JOB_DIR}/${name}.state.json"
    fi
    ;;
  logs)
    [[ $# -eq 2 || $# -eq 3 ]] || usage
    name=$2
    validate_name "${name}"
    lines=${3:-200}
    [[ ${lines} =~ ^[1-9][0-9]*$ ]] || usage
    journalctl --unit "radar-job@${name}.service" --lines "${lines}" --no-pager
    ;;
  list)
    [[ $# -eq 1 ]] || usage
    find "${JOB_DIR}" -maxdepth 1 -type f -name '*.json' ! -name '*.state.json' -printf '%f\n' \
      | sed 's/\.json$//' \
      | sort
    ;;
  *) usage ;;
esac
