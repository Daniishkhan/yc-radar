# YC Radar

YC Radar is a local, script-first hiring pipeline. It maintains neutral company identities,
attaches public job sources such as YC, Greenhouse, and Ashby, synchronizes normalized openings,
and produces an engineering shortlist.

Postgres is the source of truth. The product interface is a set of scripts plus local CSV/JSON
outputs; the repository does not run an API or web application.

## Documentation

- [System design and operations](docs/ARCHITECTURE.md)
- [Engineering constitution](docs/CONSTITUTION.md)
- [Agent guide](AGENTS.md)
- [Optional AWS worker](infra/aws/README.md)

## Quick Start

Requirements: Python 3.11+, `uv`, and Docker.

```bash
cp .env.example .env
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head
uv run python scripts/load_snapshots.py
```

`load_snapshots.py` loads the checked-in YC company and job snapshots. A clean rebuild is:

```bash
uv run python scripts/reset_database.py --yes --rebuild-schema
uv run python scripts/load_snapshots.py
```

## Normal Source Workflow

Register a company and attach a supported source:

```bash
uv run python scripts/register_company.py \
  --name "Example" \
  --website https://example.com

uv run python scripts/register_job_source.py \
  --company-slug example \
  --source-url https://job-boards.greenhouse.io/example
```

Synchronize registered Greenhouse and Ashby boards:

```bash
uv run python scripts/sync_job_sources.py --delay-seconds 2
```

Useful restrictions include `--provider`, `--company-id`, repeated `--source-id`, and `--limit`.
Each adapter returns a complete `SourceSnapshot`; `JobSyncService` applies it to canonical jobs and
records the attempt in `sync_runs`.

Refresh the checked-in YC snapshots and load the result:

```bash
uv run python scripts/extract_yc_companies.py
```

## Large or Noisy URL Imports

`scripts/stage_ingest.py` is the durable staging interface for Common Crawl files, vendor exports,
and other unreliable URL inventories. CSV and JSONL inputs stream in committed batches. Each row
needs `url`; useful optional fields are `observation_key`, `observed_at`, `priority`, `company_id`,
and `company_name`.

```bash
ingest_source=commoncrawl
ingest_run_key=source-import
ingest_input=data/local/debug/source-urls.jsonl

uv run python scripts/stage_ingest.py load \
  --input "${ingest_input}" \
  --source "${ingest_source}" \
  --run-key "${ingest_run_key}" \
  --batch-size 500

uv run python scripts/stage_ingest.py work --stage fetch \
  --source "${ingest_source}" --run-key "${ingest_run_key}" --limit 25
uv run python scripts/stage_ingest.py work --stage parse \
  --source "${ingest_source}" --run-key "${ingest_run_key}" --limit 100
uv run python scripts/stage_ingest.py work --stage enrich \
  --source "${ingest_source}" --run-key "${ingest_run_key}" --limit 100
uv run python scripts/stage_ingest.py promote \
  --source "${ingest_source}" --run-key "${ingest_run_key}" --limit 25
uv run python scripts/stage_ingest.py status \
  --source "${ingest_source}" --run-key "${ingest_run_key}"
```

Always scope worker commands with both `--source` and `--run-key`; omitting both intentionally acts
on the global queue. Each `work` or `promote` call claims at most `--limit` rows. Network batches
should stay small enough for the lease period. Repeat a stage while work is ready, then inspect
`status`: `claimed: 0` can also mean retry rows are waiting for backoff. Multiple workers may run
concurrently because claims use database leases and `FOR UPDATE SKIP LOCKED`.

Staging preserves missing/malformed URLs as raw errors, globally deduplicates normalized URL work,
stores large HTTP bodies in the local disk cache, and promotes only a complete valid provider
snapshot. Malformed JSON, timestamps, or priorities stop the input load and should be fixed at the
producer. Use `requeue` to recover expired leases or explicitly retry quarantined/dead work. A
provided `company_id` is trusted identity evidence, so supply it only after independent
verification. Once a board is registered, use `sync_job_sources.py` for recurring freshness.

The Python-level staging interfaces live in `src/yc_radar/services/staging.py`:
`StagingRepository`, `StagingWorker`, `SnapshotPromoter`, and `FunnelReporter`.

## Greenhouse Discovery with Common Crawl

Query a bounded Common Crawl URL Index partition through Athena:

```bash
uv run python scripts/query_commoncrawl_greenhouse.py
```

Without `--crawl`, the command selects the latest published crawl and prints the output and
checkpoint-manifest paths.

Combine several crawl exports while retaining crawl provenance:

```bash
uv run python scripts/union_commoncrawl_greenhouse.py \
  data/local/debug/greenhouse-candidates-crawl-one.csv \
  data/local/debug/greenhouse-candidates-crawl-two.csv \
  --output data/local/debug/greenhouse-candidates-union.csv
```

Verify candidates sequentially and optionally register complete boards:

```bash
uv run python scripts/scout_greenhouse_sources.py \
  --input data/local/debug/greenhouse-candidates-union.csv \
  --output data/local/debug/greenhouse-sources.csv \
  --apply
```

Ambiguous identity evidence never causes a silent company merge. A verified board may instead be
attached to an isolated provisional company until stronger evidence is available.

## Results

Export normalized active jobs:

```bash
uv run python scripts/generate_job_opportunities.py --limit 100
```

Generate the deterministic shortlist without paid verification or LLM refinement:

```bash
uv run python scripts/generate_weekly_targets.py \
  --no-verify-hiring \
  --no-llm \
  --limit 20
```

Outputs go under ignored `data/local/runs/`. Remote eligibility is evidence-based: explicit
Pakistan/worldwide, regional, restricted, unclear, onsite, and missing evidence remain distinct.
Unknown eligibility stays available for research rather than being silently discarded.

Personal data and generated artifacts stay local:

- `data/local/resume/`
- `data/local/profile/`
- `data/local/runs/`
- `data/local/cache/`
- `data/local/debug/`

## Add a Provider

1. Implement the read-only adapter contract in `src/yc_radar/adapters/`.
2. Emit stable `NormalizedJob` IDs in a `SourceSnapshot`.
3. Register URL detection with `default_job_source_providers()`.
4. Add mocked adapter, identity, and shared lifecycle tests.

Do not add provider-specific company/job tables. Free and paid sources use the same
company-source/job contract.

## Verification

```bash
uv run pytest
uv run alembic check
uv run ruff check src tests scripts migrations
git diff --check
```

Network calls are mocked in tests. Postgres integration tests use isolated temporary databases.
Use `uv`; there is no pip workflow.
