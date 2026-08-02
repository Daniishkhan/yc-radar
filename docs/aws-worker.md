# AWS worker operations

The production worker is one on-demand EC2 instance running Docker Compose. It has no inbound
security-group rules, no public port 22, and no EC2 SSH key. Tailscale SSH is the primary human
access path after one-time device enrollment; the same node can optionally provide stable public
egress as a Tailscale exit node through its Elastic IP. AWS Systems Manager remains the no-inbound
path for automation, initial enrollment, and break-glass recovery. Postgres, Docker state, caches,
job checkpoints, and ignored `data/local/` artifacts live on a separate encrypted 100 GiB gp3
volume mounted at `/srv/radar`. CloudFormation retains that volume and the versioned state bucket
if the instance or stack is removed, but releases the stack-owned Elastic IP automatically.

This is intentionally a worker, not a served application. Nothing listens on a public port.

## Provision

The current account already has a usable public subnet and the bounded Common Crawl Athena
workgroup. Validate and deploy the checked-in stack:

```bash
aws cloudformation validate-template \
  --profile radar-athena \
  --region us-east-1 \
  --template-body file://infra/aws/worker-stack.yaml

aws cloudformation deploy \
  --profile radar-athena \
  --region us-east-1 \
  --stack-name radar-worker \
  --template-file infra/aws/worker-stack.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    VpcId=vpc-08cd49586dc07daaa \
    SubnetId=subnet-0b0a78485cfd54ae2 \
    AvailabilityZone=us-east-1a \
    AthenaResultsBucketName=radar-athena-results-211236627350-us-east-1 \
    StateBucketName=radar-worker-state-211236627350-us-east-1
```

The stack output named `ElasticIpAddress` is the worker's stable public egress address.
`DeploymentDocumentName` and `GitHubActionsDeployRoleArn` identify the bounded, keyless production
deployment path. If this AWS account already has a GitHub Actions OIDC provider, pass its ARN as
`GitHubOidcProviderArn`; otherwise the stack creates and owns one.

The default is Ubuntu 24.04 amd64 on `t3.large`, which leaves memory headroom for local Postgres and
in-memory EDA jobs. `t3.medium` remains an allowed lower-cost option for lighter workloads. The
instance uses a 20 GiB encrypted disposable root disk and a 100 GiB encrypted retained data disk.
The bootstrap installs Docker, the Compose plugin, AWS CLI, SSM Agent, Tailscale from its official
Ubuntu repository, the systemd units, and the repository. It starts `tailscaled` without enrolling
the device and generates the Postgres password locally on the retained disk; it never copies
workstation AWS credentials, Google service-account keys, or private profile/resume files.

Get the instance ID and wait for SSM. This path is needed for initial Tailscale enrollment and
remains available for recovery:

```bash
instance_id=$(aws cloudformation describe-stacks \
  --profile radar-athena \
  --region us-east-1 \
  --stack-name radar-worker \
  --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue' \
  --output text)

aws ssm describe-instance-information \
  --profile radar-athena \
  --region us-east-1 \
  --filters "Key=InstanceIds,Values=${instance_id}"

aws ssm start-session \
  --profile radar-athena \
  --region us-east-1 \
  --target "${instance_id}"
```

Inside the SSM session, inspect the bootstrap and deployment:

```bash
sudo tail -n 200 /var/log/radar-bootstrap.log
sudo systemctl status radar-deploy --no-pager
sudo docker compose \
  --project-directory /srv/radar/app \
  --env-file /srv/radar/config/runtime.env \
  -f /srv/radar/app/compose.prod.yml \
  ps
```

## Human access with Tailscale SSH

Keep the security group at zero ingress. Tailscale reaches the host without a public SSH listener,
so do not add a TCP/22 ingress rule or an EC2 key pair. SSM remains available independently for
the checked-in deployment/job automation and for recovery if tailnet access fails.

On each new or replacement instance, the bootstrap installs and starts the Tailscale client. Use
the SSM session above to activate the device once:

```bash
sudo tailscale up --ssh --hostname=radar-worker --accept-dns=false --accept-routes=false
```

Open the URL emitted by `tailscale up`, have a tailnet administrator sign in to the intended
tailnet, and approve `radar-worker` if device approval is enabled. The administrator must configure
Tailscale SSH policy/grants that allow only the intended operator identities to connect as
`ubuntu`; being able to see the node in the tailnet is not sufficient authorization.

From an approved workstation on the tailnet, use:

```bash
tailscale ssh ubuntu@radar-worker
# Or, when the workstation's ACL/SSH configuration and name resolution permit it:
ssh ubuntu@radar-worker
```

Do not put a Tailscale auth key in Git, CloudFormation UserData, stack parameters, or deployment
scripts. Interactive login keeps reusable enrollment credentials out of the machine bootstrap.
For a stopped/expired node, failed login, or policy mistake, recover with SSM:

```bash
aws ssm start-session \
  --profile radar-athena \
  --region us-east-1 \
  --target "${instance_id}"

sudo tailscale status
sudo systemctl status tailscaled --no-pager
sudo systemctl restart tailscaled
```

Re-run the one-time `tailscale up` command if re-authentication is required. Never open public
port 22 as a recovery shortcut.

The worker security group allows all outbound traffic, including the UDP Tailscale uses for NAT
traversal and direct peer connections. That broader egress also makes exit-node forwarding
possible while the group retains zero ingress rules. Inspect Tailscale health without opening a
shell:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 tailscale
```

## Optional Tailscale exit node

The Elastic IP makes the worker's internet egress address stable, which is useful when an external
service allowlists the exit node's public IP. It does not open the instance to the internet. Keep
the worker security group at zero ingress and keep public SSH disabled; SSM is still the recovery
path if Tailscale routing or authentication fails.

Exit-node support changes the outbound security boundary. Forwarded tailnet traffic can target
arbitrary protocols and ports, so the worker security group must allow arbitrary outbound traffic
while the exit-node capability is enabled. Only intended tailnet identities should be permitted to
use the node.

AWS charges `$0.005` per hour for each public IPv4 address, whether the Elastic IP is attached and
in use or idle. That is roughly `$3.60` for 30 days; verify the current amount on
[AWS VPC pricing](https://aws.amazon.com/vpc/pricing/). AWS currently includes the first 100 GB per
month of internet data transfer out in aggregate across eligible services, then charges current
data-transfer-out rates. Browsing, downloads, streaming, and every other client flow routed through
the worker can contribute to that total; check
[EC2 data-transfer pricing](https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer) before
using it for high-volume traffic.

Bootstrap persists Linux IPv4 and IPv6 forwarding in `/etc/sysctl.d/99-tailscale.conf` but does not
advertise an unauthenticated device. The template also disables EC2 source/destination checking so
AWS permits the instance to forward traffic. After completing the interactive enrollment above,
advertise the worker with the checked-in SSM action:

```bash
./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  exit-node
```

That action invokes `sudo radar-configure-tailscale-exit-node --advertise`, which reapplies the
forwarding sysctls and runs `sudo tailscale set --advertise-exit-node`. It is safe to rerun after a
deployment or Tailscale re-authentication.

In the Tailscale admin console, approve the exit-node route advertised by `radar-worker`. Each
client must then explicitly select `radar-worker` as its exit node in the Tailscale UI or CLI:

```bash
tailscale set --exit-node=radar-worker
```

Node-key expiry is intentionally fail-closed. If the worker's key expires while a client still
selects it, that client may lose internet connectivity until it selects another exit node, turns
exit-node use off, or the worker re-authenticates. For a dedicated, locked-down server that must
route continuously, a tailnet administrator can disable key expiry in the Tailscale admin console
after accepting the longer-lived credential risk. Preserve SSM access and use it to inspect
`tailscale status`, restart `tailscaled`, check the forwarding sysctls, or re-authenticate without
opening public port 22.

## Deploy a new commit

Push `main`, then deploy its exact full commit SHA through the stack's bounded SSM document. The
host verifies that this is still the remote branch tip before it fast-forwards, rebuilds the app
image, starts Postgres, and applies Alembic migrations:

```bash
revision=$(git rev-parse HEAD)
./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  deploy "${revision}"
```

`radar-deploy` refuses a dirty machine checkout and takes a host deployment lock. It also fails
closed when any managed `radar-job@` unit is active; a deployment never stops a long-running job.
The persistent Postgres volume is not rebuilt by a code deployment.

The checked-in `CI` workflow continues to run for every push and pull request. It deploys only when
manually dispatched from `main`, and only after the same run passes locked dependency installation,
migration upgrade/check, pytest, and Ruff. Configure the GitHub `production` environment to allow
only `main` (and optionally require approval), then set environment variables `AWS_ACCOUNT_ID`,
`AWS_REGION`, `AWS_STACK_NAME`, and `AWS_DEPLOY_ROLE_ARN`; use the
`GitHubActionsDeployRoleArn` stack output for the last value. These are identifiers, not secrets.

GitHub exchanges its environment-bound OIDC token for one-hour AWS credentials. The trusted
subject is exactly `repo:Daniishkhan/yc-radar:environment:production`. The role can describe only
this CloudFormation stack, invoke only its deployment document on only its worker instance, and
read the command result. It cannot open an SSM shell or invoke `AWS-RunShellScript`. Bootstrap the
role and document once with an administrator-run CloudFormation update. Before that update, deploy
this revision once through the existing administrator-controlled path so the installed
`radar-deploy` supports `--revision`; the bounded document intentionally has no legacy fallback.
Actions cannot grant itself AWS access.

## Keyless Vertex AI from the AWS worker

The domain resolver uses Vertex AI through AWS-to-Google Workload Identity Federation. The live
Google Cloud project is `ai-project-jul-19` (`64318475882`), and the only Google identity used by
the worker is the dedicated `radar-domain-resolver` service account with
`roles/aiplatform.user`. The `radar-aws/radar-worker` provider accepts account `211236627350` only
when `GetCallerIdentity` reports the exact assumed-role ARN for the stack's `WorkerRole`. The
current physical name is `radar-worker-WorkerRole-NOqPlF4c7Z8Z`.

Provisioning is explicit-apply and derives the physical role from CloudFormation, including after
a role replacement. Run it from an administrator workstation with both `gcloud` and `aws`:

```bash
./infra/gcp/provision-vertex-wif.sh \
  --project ai-project-jul-19 \
  --aws-profile radar-athena \
  --aws-region us-east-1 \
  --output data/local/gcp/gcp-wif.json \
  --apply
```

This enables the required APIs, reconciles the dedicated service account, grants only Vertex AI
User plus the exact external principal's Workload Identity User binding, and generates an IMDSv2
external-account ADC file. It does not create a service-account key. The JSON contains no secret,
but keep it out of Git and the image so its trusted endpoints and audience remain deployment
configuration.

After deploying the host helper, atomically validate/install the file over SSM and inspect the
effective configuration:

```bash
./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  gcp-wif ai-project-jul-19 data/local/gcp/gcp-wif.json

./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  gcp-wif-status
```

The retained host copy is `/srv/radar/config/gcp/gcp-wif.json`. Compose mounts its directory
read-only at `/etc/radar`, allowing the UID-1000 app process to read
`/etc/radar/gcp-wif.json` without baking it into the image. Runtime defaults are:

```dotenv
GOOGLE_CLOUD_PROJECT=ai-project-jul-19
GOOGLE_CLOUD_LOCATION=global
YC_RADAR_VERTEX_MODEL=gemini-3.5-flash-lite
GOOGLE_APPLICATION_CREDENTIALS=/etc/radar/gcp-wif.json
```

The container obtains temporary AWS credentials from IMDSv2, exchanges them through Google's STS,
then impersonates the dedicated service account for a short-lived Google access token. The EC2
metadata hop limit is two so the container can complete IMDSv2; no AWS or Google long-lived key is
installed. See [`infra/gcp/README.md`](../infra/gcp/README.md) for reconciliation, trust-boundary,
and billing-budget details.

### Calibrate domain resolution before the full run

Use the same immutable scout CSV and raw-response cache for both phases, but separate output/status
paths because the resumability manifest fingerprints the selected limit. Do not add `--apply`
during calibration or the full evidence-gathering run. Start with 100 pending companies:

```bash
./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  run domain-resolver-calibration -- \
  python scripts/resolve_greenhouse_domains.py \
  --input /app/data/local/debug/greenhouse_board_verification_CC-MAIN-2026-30.csv \
  --output /app/data/local/debug/greenhouse_domain_resolution.calibration.csv \
  --status-file /app/data/local/debug/greenhouse_domain_resolution.calibration.status.json \
  --cache-file /app/data/local/cache/greenhouse_domain_resolver.json \
  --limit 100
```

Follow the managed job and review the durable CSV/status before authorizing more requests:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 \
  status domain-resolver-calibration
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 \
  logs domain-resolver-calibration 300
```

The calibration artifacts are retained on EBS:

- `/srv/radar/app/data/local/debug/greenhouse_domain_resolution.calibration.csv`;
- `/srv/radar/app/data/local/debug/greenhouse_domain_resolution.calibration.status.json`;
- `/srv/radar/app/data/local/cache/greenhouse_domain_resolver.json`.

For the 100 rows, inspect result/error distributions, citations and deterministic evidence, request
attempts, input/output/total tokens, and `search_query_count` plus the recorded `search_queries`
from Vertex `webSearchQueries`. Search-query count matters independently of request count: one
Gemini request can issue multiple billable searches. Google output and generated search queries
only propose domains. Automatic acceptance additionally requires all three deterministic signals:
compatible company/domain naming, brand evidence on the fetched company site, and an exact board
token in a public Greenhouse link or active inline integration script. A branded third-party job
page must remain unresolved. Page redirects are followed only through bounded HTTP(S) hops with
validated ports and public literal hosts. Inspect `company_domain_matches`, page-level errors, and
the exact Greenhouse proof in `candidate_evidence`, then inspect the atomic aggregate status
directly:

```bash
sudo jq '{
  selected, processed, succeeded, failed, resumed,
  accepted, ambiguous, manual_review, unresolved,
  network_requests, cache_hits, request_attempt_count, search_query_count,
  prompt_token_count, candidates_token_count, thoughts_token_count,
  cached_content_token_count, total_token_count
}' /srv/radar/app/data/local/debug/greenhouse_domain_resolution.calibration.status.json
```

Estimate the calibration's model cost and maximum search overage from the durable per-row metrics:

```bash
sudo python3 - \
  /srv/radar/app/data/local/debug/greenhouse_domain_resolution.calibration.csv <<'PY'
from collections import Counter
import csv
import sys


def integer(row, key):
    return int(row.get(key) or 0)


with open(sys.argv[1], newline="", encoding="utf-8") as source:
    rows = list(csv.DictReader(source))
prompt = sum(integer(row, "prompt_token_count") for row in rows)
cached = sum(integer(row, "cached_content_token_count") for row in rows)
candidates = sum(integer(row, "candidates_token_count") for row in rows)
thoughts = sum(integer(row, "thoughts_token_count") for row in rows)
queries = sum(integer(row, "search_query_count") for row in rows)
attempts = sum(integer(row, "request_attempt_count") for row in rows)
model_usd = max(prompt - cached, 0) * 0.30 / 1_000_000
model_usd += cached * 0.03 / 1_000_000
model_usd += (candidates + thoughts) * 2.50 / 1_000_000
maximum_search_overage_usd = queries * 14 / 1_000
outcomes = Counter(row.get("domain_resolution_status") or "missing" for row in rows)
print(f"rows={len(rows)} attempts={attempts} search_queries={queries}")
print(f"tokens: prompt={prompt} cached={cached} candidates={candidates} thoughts={thoughts}")
print(f"estimated_model_usd={model_usd:.6f}")
print(f"maximum_search_overage_usd={maximum_search_overage_usd:.6f}")
print("outcomes=" + ", ".join(f"{key}:{value}" for key, value in sorted(outcomes.items())))
PY
```

The search figure assumes every query is beyond the monthly allowance, so it is a conservative
upper bound, not an invoice. Confirm the raw `search_queries`, cache behavior, and accepted/rejected
domains manually before continuing.

When calibration quality and projected spend are acceptable, start a new full-run manifest and
output while retaining the same cache. The first 100 inputs replay cached raw responses without
new Vertex calls, then the resolver advances through the remaining input:

```bash
./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  run domain-resolver-full -- \
  python scripts/resolve_greenhouse_domains.py \
  --input /app/data/local/debug/greenhouse_board_verification_CC-MAIN-2026-30.csv \
  --output /app/data/local/debug/greenhouse_domain_resolution.csv \
  --status-file /app/data/local/debug/greenhouse_domain_resolution.status.json \
  --cache-file /app/data/local/cache/greenhouse_domain_resolver.json \
  --company-timeout-seconds 120
```

If the process or VM stops, use `retry domain-resolver-full`; systemd reuses the exact argv and the
resolver advances from its CSV/cache checkpoint. Each company also has a 120-second wall-clock
budget by default. A company that exceeds it is immediately checkpointed as a retryable
`request_failed` row so one bad site cannot block the remaining queue; a later retry revisits that
row while resuming completed rows. Replay checkpoints overlay updated rows onto the longer saved
checkpoint, so another interruption during replay does not discard its unvisited tail. Do not
delete the partial output or change input, model, prompt, or scope mid-run. Re-run the status and
cost checks against the full-run paths. Registry writes remain a separate reviewed `--apply`
decision.

The manifest also fingerprints the resolver prompt and deterministic evidence versions. A prompt
change intentionally creates new Vertex cache keys; an evidence-only change can reuse raw model
responses but must not resume CSV rows produced by the older acceptance rules. Use a new output
path when comparing resolver versions so the prior calibration remains auditable.

As of 2026-07-31, standard global `gemini-3.5-flash-lite` pricing is `$0.30` per million input
tokens and `$2.50` per million output tokens. Gemini 3 grounding includes 5,000 Google Web/Image
Search queries per month across the project, then charges `$14` per 1,000 individual queries;
grounding-supplied input tokens are not charged. Verify the current
[Vertex AI pricing](https://cloud.google.com/vertex-ai/generative-ai/pricing) before each large run.
Estimate model cost from the recorded token totals and retain the raw query count even while it is
inside the monthly allowance. Cloud Billing is authoritative because the free query pool is shared
with every Gemini 3 workload in the project.

A billing budget/alert is recommended, but the current Google user lacks billing-account IAM to
create or list one. This does not block WIF or the calibration. A billing administrator can add a
project-scoped budget in the Cloud Billing console; budgets alert on delayed billing data and do
not cap requests immediately.

## Run and resume jobs

`radar-jobctl` stores an exact JSON argv specification under `/srv/radar/state/jobs`, mirrors the
specification and small state summary to the versioned state bucket, and runs the command in the
app container through `radar-job@.service`. A failing or interrupted unit retries the identical
command after 60 seconds. Successful jobs disable themselves so a reboot does not rerun a finished
batch. Job outputs, manifests, caches, and status files must use `/app/data/local/...`, which is on
the retained volume.

Greenhouse scout (CSV checkpoint plus an input/scope fingerprint):

```bash
sudo radar-jobctl run greenhouse-scout -- \
  python scripts/scout_greenhouse_sources.py \
  --input /app/data/local/debug/greenhouse_board_candidates_CC-MAIN-2026-30.csv \
  --output /app/data/local/debug/greenhouse_board_verification_CC-MAIN-2026-30.csv \
  --status-file /app/data/local/runs/greenhouse-scout/status.json \
  --checkpoint-every 25 \
  --delay-seconds 1 \
  --apply
```

Provider synchronization (immutable source set, per-source attempts, and Postgres advisory lock):

```bash
sudo radar-jobctl run source-sync -- \
  python scripts/sync_job_sources.py sync \
  --provider greenhouse \
  --checkpoint-file /app/data/local/runs/source-sync/checkpoint.json \
  --status-file /app/data/local/runs/source-sync/status.json \
  --delay-seconds 1 \
  --max-attempts 4
```

One Common Crawl partition (Athena query IDs and atomic S3 download are checkpointed):

```bash
sudo radar-jobctl run greenhouse-catalog-2026-30 -- \
  python scripts/query_commoncrawl_greenhouse.py \
  --crawl CC-MAIN-2026-30 \
  --output /app/data/local/debug/greenhouse_board_candidates_CC-MAIN-2026-30.csv \
  --manifest /app/data/local/runs/greenhouse-catalog-2026-30/athena.json
```

Pipeline branches (the provider sync receives a stable checkpoint inside this status directory):

```bash
sudo radar-jobctl run pipeline -- \
  python scripts/run_pipeline.py \
  --status-dir /app/data/local/runs/pipeline-current
```

Operate jobs:

```bash
sudo radar-jobctl list
sudo radar-jobctl status greenhouse-scout
sudo radar-jobctl logs greenhouse-scout 200
sudo radar-jobctl stop greenhouse-scout
sudo radar-jobctl retry greenhouse-scout
```

The durable correctness checkpoints are distinct from systemd retry:

- Greenhouse scouting loads its `.partial.csv`, rejects an input/scope mismatch, skips terminal
  tokens, and retries failed or transient homepage verification rows.
- ATS synchronization freezes the original source IDs, skips completed sources, turns an orphaned
  `running` attempt into an audited failure, and creates `attempt-N` for the retry.
- Common Crawl querying persists deterministic request tokens and Athena query IDs before polling,
  so a restart attaches to an existing query instead of paying for it twice.
- Career discovery and classification retain their existing Postgres queue/checkpoint semantics.

## Backup and recovery

The retained EBS volume protects against instance replacement, but it is not a backup. Create a
custom-format Postgres dump and copy it to the versioned state bucket before schema changes and at
least daily once scheduled refreshes begin:

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sudo docker exec yc-radar-postgres \
  pg_dump -U yc_radar -d yc_radar --format=custom \
  --file "/tmp/yc-radar-${stamp}.dump"
sudo docker cp \
  "yc-radar-postgres:/tmp/yc-radar-${stamp}.dump" \
  "/srv/radar/backups/yc-radar-${stamp}.dump"
sudo aws s3 cp \
  "/srv/radar/backups/yc-radar-${stamp}.dump" \
  "s3://radar-worker-state-211236627350-us-east-1/postgres/yc-radar-${stamp}.dump" \
  --region us-east-1 \
  --only-show-errors
```

Do not delete `/srv/radar`, the retained EBS volume, or the state bucket when replacing the VM.
CloudFormation reports the volume ID as `DataVolumeId` so it can be inventoried and recovered.

## Security boundary

The instance role can use SSM, the single `radar-commoncrawl` Athena workgroup, the relevant Glue
database/table, the existing Athena results bucket, the public Common Crawl index prefix, and its
own state bucket. It is not an administrator and holds no long-lived access keys.

The workstation profile currently authenticates as the AWS account root. That is acceptable only
as a short bootstrap bridge: create an administrative IAM Identity Center/IAM identity, move CLI
administration to it, then rotate and remove the root access key. Never copy that profile to EC2.

## Recommended next tasks

1. Resume and finish the current 3,586-token Greenhouse scout on the worker, then run a bounded
   retry pass for transient board/homepage failures.
2. Synchronize every newly registered Greenhouse board with one checkpointed batch and record a
   complete canonical snapshot before doing any pruning.
3. Materialize the 50-crawl Common Crawl tier as compact per-crawl token evidence, preserving
   `first_seen_crawl`, `last_seen_crawl`, `crawl_count`, and URL counts. The measured 50-crawl union
   already captures about 91% of the tokens found across 100 crawls; validate the high-yield tier
   before spending requests on the long tail.
4. Rank current software-engineering/backend/platform roles, giving extra weight only to postings
   that explicitly permit worldwide, Pakistan, South Asia, or compatible APAC remote work. Do not
   infer legal eligibility from the word “remote.”
5. Build the equivalent broad Ashby token catalog and reuse the provider registry/sync lifecycle.
6. Add a daily encrypted Postgres backup job, then schedule provider refreshes only after the first
   backfill and retry queues are stable.
7. Prune duplicates/stale evidence only from a reviewed backup and audit manifest; never prune raw
   discovery provenance or canonical job history merely to reduce row count.
