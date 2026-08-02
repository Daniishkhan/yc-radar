#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: worker-ssm.sh [AWS options] ACTION [arguments]

AWS options:
  --profile PROFILE        AWS CLI profile (optional)
  --region REGION          AWS region (default: configured region or us-east-1)
  --stack-name NAME        CloudFormation stack (default: radar-worker)
  --detach                 Return after submitting an SSM Run Command

Actions:
  shell
  health
  tailscale
  exit-node
  gcp-wif PROJECT_ID CONFIG_FILE
  gcp-wif-status
  deploy REVISION
  run NAME -- COMMAND [ARG ...]
  pipeline NAME [PIPELINE ARG ...]
  sync NAME [SYNC ARG ...]
  scout NAME INPUT OUTPUT [SCOUT ARG ...]
  start NAME
  retry NAME
  stop NAME
  status NAME
  logs NAME [LINES]
  list

`pipeline`, `sync`, and `scout` automatically use stable checkpoint/status paths
under /app/data/local/runs/NAME on the retained EBS volume.
USAGE
  exit 2
}

profile=
region=
stack_name=radar-worker
detach=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) profile=${2:-}; shift 2 ;;
    --region) region=${2:-}; shift 2 ;;
    --stack-name) stack_name=${2:-}; shift 2 ;;
    --detach) detach=true; shift ;;
    -h|--help) usage ;;
    *) break ;;
  esac
done

action=${1:-}
[[ -n ${action} ]] || usage
shift

aws_args=()
if [[ -n ${profile} ]]; then
  aws_args+=(--profile "${profile}")
fi
if [[ -z ${region} ]]; then
  region=$(aws "${aws_args[@]}" configure get region || true)
  region=${region:-us-east-1}
fi
aws_args+=(--region "${region}")

instance_id=$(aws "${aws_args[@]}" cloudformation describe-stacks \
  --stack-name "${stack_name}" \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue | [0]" \
  --output text)
if [[ -z ${instance_id} || ${instance_id} == None ]]; then
  echo "Could not resolve InstanceId from stack ${stack_name}" >&2
  exit 1
fi

validate_name() {
  if [[ ! $1 =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]; then
    echo "Invalid job name '$1'" >&2
    exit 2
  fi
}

encode_argv() {
  python3 - "$@" <<'PY'
import base64
import json
import sys

print(base64.b64encode(json.dumps(sys.argv[1:]).encode()).decode())
PY
}

wait_for_command() (
  local command_id=$1 status output error_output
  if ${detach}; then
    return
  fi

  for _ in $(seq 1 720); do
    status=$(aws "${aws_args[@]}" ssm get-command-invocation \
      --command-id "${command_id}" \
      --instance-id "${instance_id}" \
      --query Status \
      --output text 2>/dev/null || true)
    case "${status}" in
      Success|Cancelled|TimedOut|Failed|Cancelling) break ;;
      *) sleep 5 ;;
    esac
  done
  output=$(aws "${aws_args[@]}" ssm get-command-invocation \
    --command-id "${command_id}" \
    --instance-id "${instance_id}" \
    --query StandardOutputContent \
    --output text 2>/dev/null || true)
  error_output=$(aws "${aws_args[@]}" ssm get-command-invocation \
    --command-id "${command_id}" \
    --instance-id "${instance_id}" \
    --query StandardErrorContent \
    --output text 2>/dev/null || true)
  [[ -z ${output} || ${output} == None ]] || printf '%s\n' "${output}"
  [[ -z ${error_output} || ${error_output} == None ]] || printf '%s\n' "${error_output}" >&2
  if [[ ${status} != Success ]]; then
    echo "SSM command ended with status ${status:-unknown}" >&2
    return 1
  fi
)

send_command() (
  local remote_command=$1 temporary request command_id
  temporary=$(mktemp -d /tmp/radar-ssm.XXXXXX)
  trap 'rm -rf "${temporary}"' EXIT
  request=${temporary}/request.json
  python3 - "${request}" "${instance_id}" "${remote_command}" <<'PY'
import json
from pathlib import Path
import sys

Path(sys.argv[1]).write_text(json.dumps({
    "DocumentName": "AWS-RunShellScript",
    "InstanceIds": [sys.argv[2]],
    "Parameters": {"commands": [sys.argv[3]]},
    "TimeoutSeconds": 3600,
}))
PY
  command_id=$(aws "${aws_args[@]}" ssm send-command \
    --cli-input-json "file://${request}" \
    --query 'Command.CommandId' \
    --output text)
  echo "SSM command: ${command_id}"
  wait_for_command "${command_id}"
)

send_deployment() (
  local revision=$1 document_name temporary request command_id
  if [[ ! ${revision} =~ ^[0-9a-f]{40}$ ]]; then
    echo "Deployment revision must be a full lowercase Git commit SHA" >&2
    exit 2
  fi

  document_name=$(aws "${aws_args[@]}" cloudformation describe-stacks \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='DeploymentDocumentName'].OutputValue | [0]" \
    --output text)
  if [[ -z ${document_name} || ${document_name} == None ]]; then
    echo "Could not resolve DeploymentDocumentName from stack ${stack_name}" >&2
    exit 1
  fi

  temporary=$(mktemp -d /tmp/radar-ssm-deploy.XXXXXX)
  trap 'rm -rf "${temporary}"' EXIT
  request=${temporary}/request.json
  python3 - "${request}" "${instance_id}" "${document_name}" "${revision}" <<'PY'
import json
from pathlib import Path
import sys

Path(sys.argv[1]).write_text(json.dumps({
    "DocumentName": sys.argv[3],
    "InstanceIds": [sys.argv[2]],
    "Parameters": {"Revision": [sys.argv[4]]},
    "TimeoutSeconds": 3600,
}))
PY
  command_id=$(aws "${aws_args[@]}" ssm send-command \
    --cli-input-json "file://${request}" \
    --query 'Command.CommandId' \
    --output text)
  echo "SSM deployment command: ${command_id}"
  wait_for_command "${command_id}"
)

case "${action}" in
  shell)
    [[ $# -eq 0 ]] || usage
    exec aws "${aws_args[@]}" ssm start-session --target "${instance_id}"
    ;;
  health)
    [[ $# -eq 0 ]] || usage
    send_command 'cloud-init status --wait; sudo systemctl status --no-pager radar-deploy.service; sudo docker ps; findmnt /srv/radar'
    ;;
  tailscale)
    [[ $# -eq 0 ]] || usage
    send_command 'sudo systemctl status --no-pager tailscaled.service; sudo tailscale status --peers=false; sudo tailscale ip -4; sudo tailscale netcheck'
    ;;
  exit-node)
    [[ $# -eq 0 ]] || usage
    send_command 'sudo /usr/local/sbin/radar-configure-tailscale-exit-node --advertise; sudo tailscale status --peers=false'
    ;;
  gcp-wif)
    [[ $# -eq 2 ]] || usage
    project_id=$1
    config_file=$2
    if [[ ! ${project_id} =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ || ! -f ${config_file} ]]; then
      echo "A valid Google Cloud project ID and readable config file are required" >&2
      exit 2
    fi
    config_size=$(wc -c < "${config_file}")
    if [[ ${config_size} -gt 12000 ]]; then
      echo "Credential config is too large for the bounded SSM installer (${config_size} bytes)" >&2
      exit 2
    fi
    encoded=$(python3 - "${config_file}" <<'PY'
import base64
from pathlib import Path
import sys

print(base64.b64encode(Path(sys.argv[1]).read_bytes()).decode())
PY
)
    remote_command="sudo /usr/local/sbin/radar-configure-gcp-wif"
    remote_command+=" --project '${project_id}' --config-b64 '${encoded}'"
    send_command "${remote_command}"
    ;;
  gcp-wif-status)
    [[ $# -eq 0 ]] || usage
    remote_command='set -e; sudo test -r /srv/radar/config/gcp/gcp-wif.json; '
    remote_command+="sudo jq '{type, audience, service_account_impersonation_url, "
    remote_command+='credential_source: {environment_id: .credential_source.environment_id, '
    remote_command+="imdsv2: (.credential_source.imdsv2_session_token_url != null)}}'"
    remote_command+=' /srv/radar/config/gcp/gcp-wif.json; '
    remote_command+="sudo grep -E '^(GOOGLE_CLOUD_PROJECT|GOOGLE_CLOUD_LOCATION|"
    remote_command+="YC_RADAR_VERTEX_MODEL|GOOGLE_APPLICATION_CREDENTIALS)='"
    remote_command+=' /srv/radar/config/runtime.env'
    send_command "${remote_command}"
    ;;
  deploy)
    [[ $# -eq 1 ]] || usage
    send_deployment "$1"
    ;;
  run)
    [[ $# -ge 3 ]] || usage
    name=$1
    validate_name "${name}"
    shift
    [[ ${1:-} == -- ]] || usage
    shift
    encoded=$(encode_argv "$@")
    send_command "sudo /usr/local/sbin/radar-jobctl create-b64 '${name}' '${encoded}' && sudo /usr/local/sbin/radar-jobctl start '${name}'"
    ;;
  pipeline)
    [[ $# -ge 1 ]] || usage
    name=$1
    validate_name "${name}"
    shift
    encoded=$(encode_argv \
      python scripts/run_pipeline.py \
      --status-dir "/app/data/local/runs/${name}" \
      --run-key "${name}" \
      "$@")
    send_command "sudo /usr/local/sbin/radar-jobctl create-b64 '${name}' '${encoded}' && sudo /usr/local/sbin/radar-jobctl start '${name}'"
    ;;
  sync)
    [[ $# -ge 1 ]] || usage
    name=$1
    validate_name "${name}"
    shift
    encoded=$(encode_argv \
      python scripts/sync_job_sources.py sync \
      --run-key "${name}" \
      --checkpoint-file "/app/data/local/runs/${name}/sync-checkpoint.json" \
      --status-file "/app/data/local/runs/${name}/sync-status.json" \
      "$@")
    send_command "sudo /usr/local/sbin/radar-jobctl create-b64 '${name}' '${encoded}' && sudo /usr/local/sbin/radar-jobctl start '${name}'"
    ;;
  scout)
    [[ $# -ge 3 ]] || usage
    name=$1
    input=$2
    output=$3
    validate_name "${name}"
    if [[ ${input} != /app/data/local/* || ${output} != /app/data/local/* ]]; then
      echo "Scout input and output must be durable /app/data/local paths" >&2
      exit 2
    fi
    shift 3
    encoded=$(encode_argv \
      python scripts/scout_greenhouse_sources.py \
      --input "${input}" \
      --output "${output}" \
      --status-file "/app/data/local/runs/${name}/scout-status.json" \
      "$@")
    send_command "sudo /usr/local/sbin/radar-jobctl create-b64 '${name}' '${encoded}' && sudo /usr/local/sbin/radar-jobctl start '${name}'"
    ;;
  start|retry|stop|status)
    [[ $# -eq 1 ]] || usage
    validate_name "$1"
    send_command "sudo /usr/local/sbin/radar-jobctl '${action}' '$1'"
    ;;
  logs)
    [[ $# -eq 1 || $# -eq 2 ]] || usage
    validate_name "$1"
    lines=${2:-200}
    [[ ${lines} =~ ^[1-9][0-9]*$ ]] || usage
    send_command "sudo /usr/local/sbin/radar-jobctl logs '$1' '${lines}'"
    ;;
  list)
    [[ $# -eq 0 ]] || usage
    send_command 'sudo /usr/local/sbin/radar-jobctl list'
    ;;
  *) usage ;;
esac
