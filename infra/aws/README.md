# Optional AWS worker

The AWS stack runs the same script-first repository on a private EC2 worker. It is operational
capacity, not part of the data model and not a served application.

The stack provides:

- Ubuntu with no inbound security-group rules;
- Tailscale SSH for normal access and SSM for recovery;
- an instance role limited to SSM, Common Crawl Athena/S3 resources, and the state bucket;
- a retained encrypted EBS volume for Docker, Postgres, caches, and run artifacts;
- an optional Tailscale exit node with stable public egress;
- deployment and detached-job helpers;
- a daily public-source/queue refresh and an hourly complete-snapshot freshness check.

The schema and YC seed data are rebuildable from the Alembic migration head and checked-in
snapshots. Discovered source mappings and staging provenance must be backed up or rediscovered.

## Provision

```bash
./infra/aws/deploy-stack.sh \
  --profile radar-athena \
  --region us-east-1 \
  --subnet-id SUBNET_ID

./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  health
```

Bootstrap clones `main`, mounts the retained volume at `/srv/radar`, creates a random Postgres
password in `/srv/radar/config/runtime.env`, builds the app, and applies Alembic.

## Tailscale SSH

Open an SSM shell for initial enrollment:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 shell
sudo tailscale up --ssh --hostname=radar-worker --accept-dns=false --accept-routes=false
```

After the tailnet administrator approves the device and policy:

```bash
tailscale ssh ubuntu@radar-worker
```

Do not open public SSH or put Tailscale keys in the repository.

Optional exit-node advertisement:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 exit-node
```

## Deploy

The host accepts only a full tested commit SHA, requires a clean checkout, and refuses deployment
while a managed job is active.

```bash
revision=$(git rev-parse HEAD)
./infra/aws/worker-ssm.sh --profile radar-athena --region us-east-1 deploy "${revision}"
```

Pushes to `main` also deploy after CI passes by connecting through Tailscale SSH.

Successful deploys enable `radar-pipeline-refresh.timer` and
`radar-pipeline-freshness.timer`. The refresh runs daily at 02:30 UTC, synchronizes public
Greenhouse, Ashby, and Lever sources sequentially, regenerates the three queues, validates the two
job queues, and writes metrics under `data/local/runs/current/`. It does not run paid TheirStack
fetches, Firecrawl, an LLM, or application submission. Inspect the schedule and last results with:

```bash
sudo systemctl list-timers 'radar-pipeline-*'
sudo systemctl status radar-pipeline-refresh.service radar-pipeline-freshness.service
sudo journalctl -u radar-pipeline-refresh.service -n 200 --no-pager
```

The repository's clean migration baseline is intentionally incompatible with legacy production
revision `0005_job_structured_evidence`. Preserve that database and create a side-by-side target
instead of running normal Alembic against it:

```bash
cd /srv/radar/app
sudo docker compose --project-directory /srv/radar/app \
  --env-file /srv/radar/config/runtime.env \
  -f /srv/radar/app/compose.prod.yml \
  run --rm app python scripts/migrate_legacy_database.py \
    --target-database yc_radar_v2 \
    --manifest /app/data/local/runs/migration/legacy-to-v2.json \
    --yes \
    --allow-source-outage
```

This command briefly disconnects the source database so PostgreSQL can take an exact physical
clone. It never rewrites or drops the source. Inspect the manifest and target counts before
changing `POSTGRES_DB` in `runtime.env`; keep the old database and a verified dump as rollback
points.

Because the schema is intentionally rebuildable, reset a replaceable worker database from a shell
when a clean database is required:

```bash
cd /srv/radar/app
sudo docker compose --project-directory /srv/radar/app \
  --env-file /srv/radar/config/runtime.env -f compose.prod.yml \
  run --rm app python scripts/reset_database.py --yes --rebuild-schema
sudo docker compose --project-directory /srv/radar/app \
  --env-file /srv/radar/config/runtime.env -f compose.prod.yml \
  run --rm app python scripts/load_snapshots.py
```

## Detached jobs

Synchronize configured sources with a stable run prefix:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena sync greenhouse-sync-current \
  --provider greenhouse --delay-seconds 2
```

Verify a Common Crawl candidate file and optionally register sources:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena scout greenhouse-scout-current \
  /app/data/local/debug/greenhouse-candidates.csv \
  /app/data/local/debug/greenhouse-sources.csv \
  --apply --checkpoint-every 25
```

Run any other command with explicit durable output paths:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena run weekly-targets -- \
  python scripts/generate_weekly_targets.py --no-verify-hiring --no-llm --limit 20
```

Operate jobs:

```bash
./infra/aws/worker-ssm.sh --profile radar-athena list
./infra/aws/worker-ssm.sh --profile radar-athena status greenhouse-sync-current
./infra/aws/worker-ssm.sh --profile radar-athena logs greenhouse-sync-current 300
./infra/aws/worker-ssm.sh --profile radar-athena retry greenhouse-sync-current
./infra/aws/worker-ssm.sh --profile radar-athena stop greenhouse-sync-current
```

## Backup

The retained EBS volume is not a backup. For source mappings that are expensive to rediscover,
create a custom-format dump and copy it to the retained state bucket before destructive work.

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sudo docker exec yc-radar-postgres \
  pg_dump -U yc_radar -d yc_radar --format=custom \
  --file "/tmp/yc-radar-${stamp}.dump"
```

Restore a database from a verified dump when preserving discovered source mappings matters;
otherwise rebuild it from the migration head and checked-in snapshots.
