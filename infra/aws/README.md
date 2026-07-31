# AWS worker deployment

This stack creates one private operational worker, not a served application:

- Ubuntu 24.04 on `t3.medium` by default.
- no inbound security-group rules (including no public port 22), EC2 SSH key, load balancer, or
  public service;
- Tailscale SSH as the primary human access path after one-time enrollment, with no-inbound SSM
  retained for automation and break-glass recovery;
- outbound internet access for SSM, package/image downloads, and polite public-source requests;
- IMDSv2 and an instance role scoped to SSM, the `radar-commoncrawl` Athena workgroup,
  the `radar_commoncrawl` Glue database, Common Crawl's index prefix, the Athena result bucket,
  and the worker state bucket;
- a 50 GiB encrypted gp3 EBS volume mounted at `/srv/radar`, retained when the stack is deleted;
- Docker's data root, Postgres, the repository, job specifications, checkpoints, and local outputs
  on that retained volume;
- a private, encrypted, versioned S3 bucket retained for job/deployment state summaries.

The EC2 root volume is disposable. The EBS volume and S3 bucket are the recovery boundary.
CloudFormation termination protection is enabled by the deployment helper.

## Provision

The bootstrap clones `main`, so commit and push these deployment files before creating the stack.
Choose an existing public subnet with a route to an internet gateway; the instance receives a
public IP but accepts no inbound connections.

```bash
./infra/aws/deploy-stack.sh \
  --profile radar-athena \
  --region us-east-1 \
  --subnet-id subnet-0b0a78485cfd54ae2

./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  health
```

Bootstrap installs Docker Compose, the AWS CLI, and Tailscale from its official Ubuntu repository,
mounts the retained volume, generates a random local Postgres password in
`/srv/radar/config/runtime.env`, builds the app image, and applies Alembic migrations. It starts
`tailscaled` but does not enroll the machine. No application API key is copied to the machine. Do
not put credentials in CloudFormation parameters, job names, commands, or Git. Add future secrets
through an explicit SSM Parameter Store integration.

## Human access with Tailscale SSH

Tailscale SSH is the normal path for an operator shell. Keep the worker security group at zero
ingress: do not expose TCP port 22 or add an EC2 SSH key. SSM continues to provide no-inbound
automation through `worker-ssm.sh` and is the bootstrap and break-glass path if Tailscale is
unavailable.

For each new or replacement instance, first open an SSM shell:

```bash
./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  shell
```

The bootstrap has already installed and started Tailscale. Enroll the device once:

```bash
sudo tailscale up --ssh --hostname=radar-worker --accept-dns=false --accept-routes=false
```

Open the login URL printed by that command, sign in to the intended tailnet, and have a tailnet
administrator approve the device when device approval is enabled. The administrator must also
grant the intended operator identities access to the `ubuntu` account through the tailnet's
Tailscale SSH policy/grants. Tailnet membership alone does not grant a shell.

From an approved tailnet client, connect with:

```bash
tailscale ssh ubuntu@radar-worker
# Or, when the client's ACL/SSH configuration and name resolution permit it:
ssh ubuntu@radar-worker
```

Enrollment is deliberately interactive. Never store a Tailscale auth key in this repository,
CloudFormation UserData, or deployment scripts. If the node is offline, expired, or denied by an
incorrect policy, recover through SSM, inspect `sudo tailscale status` and the `tailscaled` service,
then re-authenticate or correct the tailnet policy as needed. Do not solve a Tailscale failure by
opening public port 22.

The security group permits outbound UDP so Tailscale can attempt NAT traversal and a direct
WireGuard connection. It still has no inbound rules. Check the node without opening a shell:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 tailscale
```

## Deploy a pushed revision

Deployment uses `git merge --ff-only` and refuses to overwrite tracked VM changes. It also refuses
to migrate/rebuild while a managed job is active.

```bash
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 deploy
```

## Run resumable jobs

The generic runner persists the exact argv and a state summary, mirrors both to versioned S3, and
runs the command inside the production app container. Systemd retries failures after 60 seconds,
with a maximum of five starts per hour. An interrupted/failed job remains enabled and resumes after
a reboot. A successful job disables itself so it does not run again on the next boot.

The task itself must checkpoint into Postgres or `/app/data/local`. The helpers below add stable
paths for the pipelines that support file checkpoints:

```bash
# Pipeline stage status and a stable provider run prefix.
./infra/aws/worker-ssm.sh --profile radar-athena pipeline pipeline-20260731 \
  --classification-limit 200

# Exact source manifest plus per-source attempts; safe to rerun with the same name.
./infra/aws/worker-ssm.sh --profile radar-athena sync greenhouse-sync-20260731 \
  --provider greenhouse --delay-seconds 2

# Partial CSV plus atomic status JSON on EBS. Add --apply only after candidate review.
./infra/aws/worker-ssm.sh --profile radar-athena scout greenhouse-scout-20260731 \
  /app/data/local/debug/greenhouse_candidates.csv \
  /app/data/local/debug/greenhouse_verification.csv \
  --checkpoint-every 25 --delay-seconds 1

# Any other script, provided its checkpoint/output paths are durable.
./infra/aws/worker-ssm.sh --profile radar-athena run custom-job -- \
  python scripts/example.py --status-file /app/data/local/runs/custom-job/status.json
```

The SSM command returns after the systemd unit starts; it does not wait for the long job. Inspect or
control it separately:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena status greenhouse-sync-20260731
./infra/aws/worker-ssm.sh --profile radar-athena logs greenhouse-sync-20260731 300
./infra/aws/worker-ssm.sh --profile radar-athena retry greenhouse-sync-20260731
./infra/aws/worker-ssm.sh --profile radar-athena stop greenhouse-sync-20260731
```

`retry` resets systemd's rate limit but reuses the identical job spec and checkpoints. `stop`
disables the unit; `start` enables it again. Use a new job name for a logically new batch.

## Recovery notes

- Deleting the stack requires explicitly disabling stack termination protection. The data volume
  and versioned state bucket remain and incur storage charges.
- A retained EBS volume can only attach to an instance in the same Availability Zone. Snapshot it
  before moving the worker to another Availability Zone.
- S3 currently mirrors small job/deployment manifests, not the Postgres database or all local
  artifacts. Automated `pg_dump` and selected artifact uploads are a recommended next hardening
  step.
- The instance profile deliberately has no permission to terminate instances, mutate IAM, read
  arbitrary buckets, or manage secrets.
