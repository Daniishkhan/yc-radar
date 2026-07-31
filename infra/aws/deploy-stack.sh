#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
readonly TEMPLATE=${SCRIPT_DIR}/worker-stack.yaml

usage() {
  cat >&2 <<'USAGE'
Usage: deploy-stack.sh --subnet-id SUBNET [options]

Options:
  --profile PROFILE             AWS CLI profile (optional)
  --region REGION               AWS region (default: configured region or us-east-1)
  --stack-name NAME             CloudFormation stack (default: radar-worker)
  --instance-type TYPE          EC2 type (default: t3.medium)
  --volume-size-gib SIZE        Retained gp3 size (default: 100)
  --repo-url URL                Public GitHub repository
  --repo-branch BRANCH          Branch to deploy (default: main)
  --athena-results-bucket NAME  Existing Athena output bucket
  --state-bucket NAME           New retained/versioned worker-state bucket
  --no-termination-protection   Do not enable CloudFormation termination protection

The script derives the VPC and Availability Zone from the selected subnet.
USAGE
  exit 2
}

subnet_id=
profile=
region=
stack_name=radar-worker
instance_type=t3.medium
volume_size=100
repo_url=https://github.com/Daniishkhan/yc-radar.git
repo_branch=main
athena_results_bucket=
state_bucket=
protect_stack=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subnet-id) subnet_id=${2:-}; shift 2 ;;
    --profile) profile=${2:-}; shift 2 ;;
    --region) region=${2:-}; shift 2 ;;
    --stack-name) stack_name=${2:-}; shift 2 ;;
    --instance-type) instance_type=${2:-}; shift 2 ;;
    --volume-size-gib) volume_size=${2:-}; shift 2 ;;
    --repo-url) repo_url=${2:-}; shift 2 ;;
    --repo-branch) repo_branch=${2:-}; shift 2 ;;
    --athena-results-bucket) athena_results_bucket=${2:-}; shift 2 ;;
    --state-bucket) state_bucket=${2:-}; shift 2 ;;
    --no-termination-protection) protect_stack=false; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

[[ -n ${subnet_id} ]] || usage
[[ ${volume_size} =~ ^[0-9]+$ ]] || usage

aws_args=()
if [[ -n ${profile} ]]; then
  aws_args+=(--profile "${profile}")
fi
if [[ -z ${region} ]]; then
  region=$(aws "${aws_args[@]}" configure get region || true)
  region=${region:-us-east-1}
fi
aws_args+=(--region "${region}")

read -r vpc_id availability_zone < <(
  aws "${aws_args[@]}" ec2 describe-subnets \
    --subnet-ids "${subnet_id}" \
    --query 'Subnets[0].[VpcId,AvailabilityZone]' \
    --output text
)
if [[ -z ${vpc_id} || ${vpc_id} == None || -z ${availability_zone} || ${availability_zone} == None ]]; then
  echo "Could not resolve VPC and Availability Zone for ${subnet_id}" >&2
  exit 1
fi

account_id=$(aws "${aws_args[@]}" sts get-caller-identity --query Account --output text)
athena_results_bucket=${athena_results_bucket:-radar-athena-results-${account_id}-${region}}
state_bucket=${state_bucket:-radar-worker-state-${account_id}-${region}}

aws "${aws_args[@]}" cloudformation deploy \
  --template-file "${TEMPLATE}" \
  --stack-name "${stack_name}" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    VpcId="${vpc_id}" \
    SubnetId="${subnet_id}" \
    AvailabilityZone="${availability_zone}" \
    InstanceType="${instance_type}" \
    DataVolumeSizeGiB="${volume_size}" \
    RepositoryUrl="${repo_url}" \
    RepositoryBranch="${repo_branch}" \
    AthenaResultsBucketName="${athena_results_bucket}" \
    StateBucketName="${state_bucket}"

if ${protect_stack}; then
  aws "${aws_args[@]}" cloudformation update-termination-protection \
    --stack-name "${stack_name}" \
    --enable-termination-protection \
    >/dev/null
fi

aws "${aws_args[@]}" cloudformation describe-stacks \
  --stack-name "${stack_name}" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table

echo "CloudFormation is complete. Bootstrap continues on the instance; use worker-ssm.sh health to verify it."
