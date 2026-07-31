#!/usr/bin/env bash
set -Eeuo pipefail

readonly RUNTIME_ENV=${RADAR_GCP_RUNTIME_ENV:-/srv/radar/config/runtime.env}
readonly CREDENTIALS_DIR=${RADAR_GCP_CREDENTIALS_DIR:-/srv/radar/config/gcp}
readonly CREDENTIALS_FILE=${CREDENTIALS_DIR}/gcp-wif.json
readonly CONTAINER_CREDENTIALS_FILE=/etc/radar/gcp-wif.json
readonly DEFAULT_LOCATION=global
readonly DEFAULT_MODEL=gemini-3.5-flash-lite
readonly POOL_ID=radar-aws
readonly PROVIDER_ID=radar-worker
readonly SERVICE_ACCOUNT_ID=radar-domain-resolver

usage() {
  cat >&2 <<'USAGE'
Usage:
  radar-configure-gcp-wif
  radar-configure-gcp-wif --project PROJECT_ID --config-b64 BASE64_JSON

With no options, reconcile the runtime defaults and credential mount directory. With both options,
validate and atomically install a keyless AWS external-account configuration. The JSON is not a
service-account key and contains no private key or reusable credential.
USAGE
  exit 2
}

project_id=
encoded_config=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) project_id=${2:-}; shift 2 ;;
    --config-b64) encoded_config=${2:-}; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "radar-configure-gcp-wif must run as root" >&2
  exit 1
fi
if [[ ! -f ${RUNTIME_ENV} ]]; then
  echo "Missing ${RUNTIME_ENV}" >&2
  exit 1
fi
if [[ -n ${project_id} || -n ${encoded_config} ]]; then
  [[ ${project_id} =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || usage
  [[ -n ${encoded_config} ]] || usage
  if [[ ${#encoded_config} -gt 20000 ]]; then
    echo "Credential configuration exceeds the 20,000-character installation limit" >&2
    exit 2
  fi
fi

install -d -m 0755 "${CREDENTIALS_DIR}"

if [[ -n ${encoded_config} ]]; then
  temporary_config=$(mktemp "${CREDENTIALS_DIR}/.gcp-wif.json.XXXXXX")
  trap 'rm -f "${temporary_config}"' EXIT
  python3 - \
    "${temporary_config}" \
    "${encoded_config}" \
    "${project_id}" \
    "${POOL_ID}" \
    "${PROVIDER_ID}" \
    "${SERVICE_ACCOUNT_ID}" <<'PY'
import base64
import binascii
import json
import os
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
encoded = sys.argv[2]
project_id = sys.argv[3]
pool_id = sys.argv[4]
provider_id = sys.argv[5]
service_account_id = sys.argv[6]

try:
    raw = base64.b64decode(encoded, validate=True)
    payload = json.loads(raw)
except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Invalid base64 JSON credential configuration: {exc}") from exc

audience_pattern = re.compile(
    rf"^//iam\.googleapis\.com/projects/[0-9]+/locations/global/"
    rf"workloadIdentityPools/{re.escape(pool_id)}/providers/{re.escape(provider_id)}$"
)
expected_impersonation_url = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
    f"{service_account_id}@{project_id}.iam.gserviceaccount.com:generateAccessToken"
)
expected_source = {
    "environment_id": "aws1",
    "region_url": "http://169.254.169.254/latest/meta-data/placement/availability-zone",
    "url": "http://169.254.169.254/latest/meta-data/iam/security-credentials",
    "regional_cred_verification_url": (
        "https://sts.{region}.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15"
    ),
    "imdsv2_session_token_url": "http://169.254.169.254/latest/api/token",
}

checks = {
    "type": payload.get("type") == "external_account",
    "audience": isinstance(payload.get("audience"), str)
    and audience_pattern.fullmatch(payload["audience"]) is not None,
    "subject_token_type": payload.get("subject_token_type")
    == "urn:ietf:params:aws:token-type:aws4_request",
    "service_account_impersonation_url": payload.get("service_account_impersonation_url")
    == expected_impersonation_url,
    "token_url": payload.get("token_url") == "https://sts.googleapis.com/v1/token",
    "credential_source": payload.get("credential_source") == expected_source,
}
failed = [name for name, valid in checks.items() if not valid]
if failed:
    raise SystemExit("Credential configuration failed validation: " + ", ".join(failed))

path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
os.chmod(path, 0o644)
PY
  mv -f "${temporary_config}" "${CREDENTIALS_FILE}"
fi

python3 - \
  "${RUNTIME_ENV}" \
  "${project_id}" \
  "${DEFAULT_LOCATION}" \
  "${DEFAULT_MODEL}" \
  "${CONTAINER_CREDENTIALS_FILE}" <<'PY'
import os
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
requested_project = sys.argv[2]
location = sys.argv[3]
default_model = sys.argv[4]
credentials_file = sys.argv[5]
lines = path.read_text().splitlines()
key_pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")

existing: dict[str, str] = {}
for line in lines:
    match = key_pattern.match(line)
    if match:
        existing[match.group(1)] = line.split("=", 1)[1]

values = {
    "GOOGLE_CLOUD_PROJECT": requested_project or existing.get("GOOGLE_CLOUD_PROJECT", ""),
    "GOOGLE_CLOUD_LOCATION": location,
    "YC_RADAR_VERTEX_MODEL": existing.get("YC_RADAR_VERTEX_MODEL") or default_model,
    "GOOGLE_APPLICATION_CREDENTIALS": credentials_file,
}

result: list[str] = []
emitted: set[str] = set()
for line in lines:
    match = key_pattern.match(line)
    key = match.group(1) if match else None
    if key not in values:
        result.append(line)
        continue
    if key not in emitted:
        result.append(f"{key}={values[key]}")
        emitted.add(key)
for key, value in values.items():
    if key not in emitted:
        result.append(f"{key}={value}")

temporary = path.with_suffix(".tmp")
temporary.write_text("\n".join(result) + "\n")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY

if [[ -n ${encoded_config} ]]; then
  echo "Installed keyless GCP credential configuration at ${CREDENTIALS_FILE}"
else
  echo "Reconciled GCP runtime defaults; no credential configuration was installed"
fi
