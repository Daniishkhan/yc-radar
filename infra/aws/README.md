# AWS worker deployment

This stack creates one private operational worker, not a served application:

- Ubuntu 24.04 on `t3.medium` by default.
- no inbound security-group rules (including no public port 22), EC2 SSH key, load balancer, or
  public service;
- Tailscale SSH as the primary human access path after one-time enrollment, with no-inbound SSM
  retained for automation and break-glass recovery;
- an optional Tailscale exit node with a stack-owned Elastic IP for stable public egress;
- unrestricted outbound internet access for SSM, package/image downloads, polite public-source
  requests, and arbitrary forwarded traffic when the exit node is enabled;
- IMDSv2 and an instance role scoped to SSM, the `radar-commoncrawl` Athena workgroup,
  the `radar_commoncrawl` Glue database, Common Crawl's index prefix, the Athena result bucket,
  and the worker state bucket;
- a 100 GiB encrypted gp3 EBS volume mounted at `/srv/radar`, retained when the stack is deleted;
- Docker's data root, Postgres, the repository, job specifications, checkpoints, and local outputs
  on that retained volume;
- a private, encrypted, versioned S3 bucket retained for job/deployment state summaries.
- optional keyless Vertex AI access through an exact-role Google Workload Identity Federation
  provider, with its non-secret ADC config mounted read-only from retained storage.

The EC2 root volume is disposable. The EBS volume and S3 bucket are the recovery boundary. The
Elastic IP is not retained: deleting the stack releases it automatically. CloudFormation
termination protection is enabled by the deployment helper.

## Provision

The bootstrap clones `main`, so commit and push these deployment files before creating the stack.
Choose an existing public subnet with a route to an internet gateway; the instance receives a
static Elastic IP but accepts no inbound connections.

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

The deployment output reports the stable public egress address as `ElasticIpAddress`.

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

The security group permits all outbound traffic, including the UDP Tailscale uses for NAT
traversal and direct WireGuard connections. That broader egress also makes exit-node forwarding
possible; it still has no inbound rules. Check the node without opening a shell:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 tailscale
```

## Optional Tailscale exit node

The stack-owned Elastic IP gives exit-node users a stable public egress address. It does not make
the worker publicly reachable: the security group retains zero inbound rules, public SSH remains
disabled, and SSM remains the independent recovery path. Exit-node forwarding does require
arbitrary outbound traffic because tailnet clients may connect to destinations on any protocol or
port; do not narrow the security-group egress rules while this capability is in use.

AWS bills public IPv4 addresses, including an attached or idle Elastic IP, at `$0.005` per hour
(roughly `$3.60` per 30-day month). Check [AWS VPC pricing](https://aws.amazon.com/vpc/pricing/)
before relying on that estimate. The first 100 GB per month of internet data transfer out is free
in aggregate across eligible AWS services, after which current AWS data-transfer-out rates apply.
All client traffic sent through the exit node can contribute to that usage, so streaming, large
downloads, and other high-volume traffic can cost materially more than the IP address itself; see
[EC2 data-transfer pricing](https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer).

Bootstrap enables Linux IPv4 and IPv6 forwarding persistently without advertising the node. After
the device is enrolled, use the SSM helper to reapply that forwarding configuration and run
`tailscale set --advertise-exit-node`. The template also disables EC2 source/destination checking,
which AWS requires for an instance that forwards traffic:

```bash
./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  exit-node
```

From a host shell, the equivalent managed command is
`sudo radar-configure-tailscale-exit-node --advertise`; the helper writes
`/etc/sysctl.d/99-tailscale.conf`, loads the forwarding settings, and runs
`sudo tailscale set --advertise-exit-node`.

A tailnet administrator must then approve the advertised exit-node route in the Tailscale admin
console. Advertising the node does not route any client automatically; select `radar-worker` as
the exit node on each intended client, or use:

```bash
tailscale set --exit-node=radar-worker
```

Tailscale routing fails closed if the worker's node key expires: clients that still select this
exit node can lose internet access until they select another exit node, disable exit-node use, or
the worker re-authenticates. If uninterrupted routing is required, an administrator can disable
key expiry for this locked-down server in the Tailscale admin console, accepting the longer-lived
device credential. Keep SSM working so `tailscale status`, `tailscaled`, forwarding state, and
re-authentication can be repaired without public SSH.

## Deploy a pushed revision

Deployment uses `git merge --ff-only` and refuses to overwrite tracked VM changes. It also refuses
to migrate/rebuild while a managed job is active.

```bash
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 deploy
```

## Install keyless Vertex AI credentials

Google Cloud trusts only AWS account `211236627350` and the exact physical `WorkerRole` emitted by
this stack. The checked-in GCP provisioner derives that role name, grants a dedicated
`radar-domain-resolver` service account only `roles/aiplatform.user`, and creates an AWS
external-account ADC config without a service-account key:

```bash
./infra/gcp/provision-vertex-wif.sh \
  --project ai-project-jul-19 \
  --aws-profile radar-athena \
  --aws-region us-east-1 \
  --output data/local/gcp/gcp-wif.json \
  --apply

./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 \
  gcp-wif ai-project-jul-19 data/local/gcp/gcp-wif.json
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 gcp-wif-status
```

The worker stores the config at `/srv/radar/config/gcp/gcp-wif.json` and mounts the containing
directory read-only at `/etc/radar`. Runtime defaults select project `ai-project-jul-19`, location
`global`, model `gemini-3.5-flash-lite`, and ADC path `/etc/radar/gcp-wif.json`. The file contains
resource identifiers and metadata endpoints, not a private key or token, and is never baked into
the app image. Full setup and the required 100-company cost/quality calibration are in
[`docs/aws-worker.md`](../../docs/aws-worker.md).

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
