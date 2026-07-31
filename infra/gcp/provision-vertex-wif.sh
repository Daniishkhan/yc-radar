#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/../.." && pwd)
readonly REPO_ROOT

readonly AWS_ACCOUNT_ID=211236627350
readonly SERVICE_ACCOUNT_ID=radar-domain-resolver
readonly POOL_ID=radar-aws
readonly PROVIDER_ID=radar-worker
readonly DEFAULT_OUTPUT=${REPO_ROOT}/data/local/gcp/gcp-wif.json

usage() {
  cat >&2 <<'USAGE'
Usage: provision-vertex-wif.sh --project PROJECT_ID [options] [--apply]

Options:
  --project PROJECT_ID       Google Cloud project that owns Vertex AI and WIF (required)
  --output FILE              Credential config output (default: data/local/gcp/gcp-wif.json)
  --aws-profile PROFILE      AWS CLI profile used to inspect the worker stack (optional)
  --aws-region REGION        AWS stack region (default: configured region or us-east-1)
  --aws-stack-name NAME      AWS CloudFormation stack (default: radar-worker)
  --apply                    Reconcile Google Cloud resources and write the credential config

Without --apply this command only prints the intended configuration. It never creates a service
account key. The generated external-account JSON contains no private key or reusable credential.
USAGE
  exit 2
}

project_id=
output=${DEFAULT_OUTPUT}
aws_profile=
aws_region=
aws_stack_name=radar-worker
apply=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) project_id=${2:-}; shift 2 ;;
    --output) output=${2:-}; shift 2 ;;
    --aws-profile) aws_profile=${2:-}; shift 2 ;;
    --aws-region) aws_region=${2:-}; shift 2 ;;
    --aws-stack-name) aws_stack_name=${2:-}; shift 2 ;;
    --apply) apply=true; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

[[ ${project_id} =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || usage
[[ ${aws_stack_name} =~ ^[A-Za-z][A-Za-z0-9-]{0,127}$ ]] || usage
[[ -n ${output} ]] || usage

if ! ${apply}; then
  cat <<EOF
Plan only; no cloud commands were run.

Google Cloud project: ${project_id}
Service account:       ${SERVICE_ACCOUNT_ID}@${project_id}.iam.gserviceaccount.com
WIF pool/provider:     ${POOL_ID}/${PROVIDER_ID}
Trusted AWS account:   ${AWS_ACCOUNT_ID}
AWS role source:       CloudFormation stack ${aws_stack_name}
Credential config:     ${output}

Re-run with --apply after reviewing the script and active gcloud/AWS identities.
EOF
  exit 0
fi

command -v aws >/dev/null 2>&1 || { echo "aws CLI is required" >&2; exit 1; }
command -v gcloud >/dev/null 2>&1 || { echo "gcloud CLI is required" >&2; exit 1; }

output_dir=$(dirname -- "${output}")
install -d -m 0755 "${output_dir}"
temporary_dir=$(mktemp -d "${output_dir}/.radar-gcp-wif.XXXXXX")
trap 'rm -rf -- "${temporary_dir}"' EXIT

aws_args=()
if [[ -n ${aws_profile} ]]; then
  aws_args+=(--profile "${aws_profile}")
fi
if [[ -z ${aws_region} ]]; then
  aws_region=$(aws "${aws_args[@]}" configure get region || true)
  aws_region=${aws_region:-us-east-1}
fi
aws_args+=(--region "${aws_region}")

resolved_account_id=$(aws "${aws_args[@]}" sts get-caller-identity --query Account --output text)
if [[ ${resolved_account_id} != "${AWS_ACCOUNT_ID}" ]]; then
  echo "AWS identity is in account ${resolved_account_id}; expected ${AWS_ACCOUNT_ID}" >&2
  exit 1
fi

aws_role_name=$(aws "${aws_args[@]}" cloudformation describe-stacks \
  --stack-name "${aws_stack_name}" \
  --query "Stacks[0].Outputs[?OutputKey=='WorkerRoleName'].OutputValue | [0]" \
  --output text)
if [[ -z ${aws_role_name} || ${aws_role_name} == None ]]; then
  # The fallback supports a stack created before WorkerRoleName became an output.
  aws_role_name=$(aws "${aws_args[@]}" cloudformation describe-stack-resource \
    --stack-name "${aws_stack_name}" \
    --logical-resource-id WorkerRole \
    --query StackResourceDetail.PhysicalResourceId \
    --output text)
fi
if [[ ! ${aws_role_name} =~ ^[A-Za-z0-9+=,.@_-]{1,64}$ ]]; then
  echo "Could not resolve a valid WorkerRole name from stack ${aws_stack_name}" >&2
  exit 1
fi

service_account_email=${SERVICE_ACCOUNT_ID}@${project_id}.iam.gserviceaccount.com
attribute_mapping="google.subject=assertion.arn"
attribute_mapping+=",attribute.aws_role=assertion.arn.contains('assumed-role') ? "
attribute_mapping+="assertion.arn.extract('{account_arn}assumed-role/') + 'assumed-role/' + "
attribute_mapping+="assertion.arn.extract('assumed-role/{role_name}/') : assertion.arn"
attribute_condition="assertion.account == '${AWS_ACCOUNT_ID}' && "
attribute_condition+="assertion.arn.startsWith("
attribute_condition+="'arn:aws:sts::${AWS_ACCOUNT_ID}:assumed-role/${aws_role_name}/')"

gcloud projects describe "${project_id}" --format='value(projectId)' >/dev/null
project_number=$(gcloud projects describe "${project_id}" --format='value(projectNumber)')
if [[ ! ${project_number} =~ ^[0-9]+$ ]]; then
  echo "Could not resolve the numeric project number for ${project_id}" >&2
  exit 1
fi

gcloud services enable \
  aiplatform.googleapis.com \
  cloudresourcemanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  --project="${project_id}" \
  --quiet

if ! gcloud iam service-accounts describe "${service_account_email}" \
  --project="${project_id}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SERVICE_ACCOUNT_ID}" \
    --project="${project_id}" \
    --display-name="Radar domain resolver" \
    --description="Keyless Vertex AI identity for the YC Radar AWS worker" \
    --quiet
fi

gcloud projects add-iam-policy-binding "${project_id}" \
  --member="serviceAccount:${service_account_email}" \
  --role=roles/aiplatform.user \
  --condition=None \
  --quiet >/dev/null

if gcloud iam workload-identity-pools describe "${POOL_ID}" \
  --location=global \
  --project="${project_id}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools update "${POOL_ID}" \
    --location=global \
    --project="${project_id}" \
    --display-name="Radar AWS workloads" \
    --description="Dedicated federation pool for the YC Radar worker" \
    --no-disabled \
    --quiet
else
  gcloud iam workload-identity-pools create "${POOL_ID}" \
    --location=global \
    --project="${project_id}" \
    --display-name="Radar AWS workloads" \
    --description="Dedicated federation pool for the YC Radar worker" \
    --quiet
fi

provider_args=(
  --location=global
  --workload-identity-pool="${POOL_ID}"
  --project="${project_id}"
  --account-id="${AWS_ACCOUNT_ID}"
  --attribute-mapping="${attribute_mapping}"
  --attribute-condition="${attribute_condition}"
  --display-name="Radar EC2 worker"
  --description="Only the current radar-worker EC2 role in AWS account ${AWS_ACCOUNT_ID}"
  --quiet
)
if gcloud iam workload-identity-pools providers describe "${PROVIDER_ID}" \
  --location=global \
  --workload-identity-pool="${POOL_ID}" \
  --project="${project_id}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-aws "${PROVIDER_ID}" \
    "${provider_args[@]}" \
    --no-disabled
else
  gcloud iam workload-identity-pools providers create-aws "${PROVIDER_ID}" \
    "${provider_args[@]}"
fi

principal_prefix="principalSet://iam.googleapis.com/projects/${project_number}/locations/global/"
principal_prefix+="workloadIdentityPools/${POOL_ID}/attribute.aws_role/"
principal_set="${principal_prefix}arn:aws:sts::${AWS_ACCOUNT_ID}:assumed-role/${aws_role_name}"
gcloud iam service-accounts add-iam-policy-binding "${service_account_email}" \
  --project="${project_id}" \
  --member="${principal_set}" \
  --role=roles/iam.workloadIdentityUser \
  --condition=None \
  --quiet >/dev/null

policy_file=${temporary_dir}/service-account-policy.json
stale_members_file=${temporary_dir}/stale-members.txt
gcloud iam service-accounts get-iam-policy "${service_account_email}" \
  --project="${project_id}" \
  --format=json > "${policy_file}"
python3 - \
  "${policy_file}" \
  "${principal_prefix}" \
  "${principal_set}" \
  "${stale_members_file}" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text())
prefix = sys.argv[2]
current = sys.argv[3]
stale_members: list[str] = []
for binding in payload.get("bindings", []):
    if binding.get("role") != "roles/iam.workloadIdentityUser":
        continue
    stale_members.extend(
        member
        for member in binding.get("members", [])
        if member.startswith(prefix) and member != current
    )
Path(sys.argv[4]).write_text("\n".join(stale_members) + ("\n" if stale_members else ""))
PY
while IFS= read -r stale_member; do
  [[ -n ${stale_member} ]] || continue
  gcloud iam service-accounts remove-iam-policy-binding "${service_account_email}" \
    --project="${project_id}" \
    --member="${stale_member}" \
    --role=roles/iam.workloadIdentityUser \
    --condition=None \
    --quiet >/dev/null
done < "${stale_members_file}"

temporary_output=${temporary_dir}/gcp-wif.json

gcloud iam workload-identity-pools create-cred-config \
  "projects/${project_number}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}" \
  --service-account="${service_account_email}" \
  --aws \
  --enable-imdsv2 \
  --output-file="${temporary_output}"
chmod 0644 "${temporary_output}"
mv -f "${temporary_output}" "${output}"

cat <<EOF
Reconciled keyless Vertex AI access for:
  service account: ${service_account_email}
  AWS role:        arn:aws:iam::${AWS_ACCOUNT_ID}:role/${aws_role_name}
  credential file: ${output}

No service-account key was created. Install the non-secret config on the worker with:
  ./infra/aws/worker-ssm.sh --profile PROFILE --region ${aws_region} gcp-wif ${project_id} ${output}
EOF
