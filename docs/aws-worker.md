# AWS worker operations

The production worker is one on-demand EC2 instance running Docker Compose. It has no inbound
security-group rules, no public port 22, and no EC2 SSH key. Tailscale SSH is the primary human
access path after one-time device enrollment; AWS Systems Manager remains the no-inbound path for
automation, initial enrollment, and break-glass recovery. Postgres, Docker state, caches, job
checkpoints, and ignored `data/local/` artifacts live on a separate encrypted 50 GiB gp3 volume
mounted at `/srv/radar`. CloudFormation retains that volume and the versioned state bucket if the
instance or stack is removed.

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

The default is Ubuntu 24.04 amd64 on `t3.medium`, a 20 GiB encrypted disposable root disk, and a
50 GiB encrypted retained data disk. The bootstrap installs Docker, the Compose plugin, AWS CLI,
SSM Agent, Tailscale from its official Ubuntu repository, the systemd units, and the repository.
It starts `tailscaled` without enrolling the device and generates the Postgres password locally on
the retained disk; it never copies workstation AWS credentials or private profile/resume files.

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

The worker security group allows outbound UDP for Tailscale NAT traversal and direct peer
connections while retaining zero ingress rules. Inspect Tailscale health without opening a shell:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 tailscale
```

## Deploy a new commit

Push `main`, then ask the host to fast-forward, rebuild the app image, start Postgres, and apply
Alembic migrations:

```bash
sudo radar-deploy
```

`radar-deploy` refuses a dirty machine checkout and takes a host deployment lock. The persistent
Postgres volume is not rebuilt by a code deployment.

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
