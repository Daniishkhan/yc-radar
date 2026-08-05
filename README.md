# YC Radar

YC Radar is a local, script-first hiring pipeline. It maintains neutral company identities,
attaches public job sources such as YC, Greenhouse, Ashby, and Lever, synchronizes normalized
openings, and produces an engineering shortlist.

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

Synchronize registered Greenhouse, Ashby, and Lever boards:

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

## TheirStack Job Discovery

TheirStack is a bounded vendor-search evidence source. Put its token in the ignored `.env` file as
`THEIRSTACK_API_KEY`; do not pass the token on the command line or copy it into run artifacts.
Imported jobs are observation-confidence source assertions: they may add or refresh jobs, but they
are not a complete company snapshot and cannot infer absence or close a job. Ranking and normal
exports treat an observation as fresh for 45 days from its last-seen timestamp; this read-time
window does not rewrite or close the stored job. Adjust exports with
`--observation-max-age-days DAYS`, or use `--no-observation-age-filter` only when deliberately
inspecting older observations.

The operator flow is deliberately split so a retry cannot silently spend credits again. Inspect
each subcommand's `--help` for its query, run, and credit flags:

```bash
theirstack_manifest="data/local/runs/theirstack/$(date +%F)/manifest.json"

uv run python scripts/import_theirstack_jobs.py preview \
  --manifest "${theirstack_manifest}" \
  --pages-per-stratum 4

theirstack_budget="$(jq -r '.credit_budget' "${theirstack_manifest}")"
uv run python scripts/import_theirstack_jobs.py fetch \
  --manifest "${theirstack_manifest}" \
  --max-credits "${theirstack_budget}" \
  --yes-spend-credits

uv run python scripts/import_theirstack_jobs.py apply \
  --manifest "${theirstack_manifest}"

uv run python scripts/import_theirstack_jobs.py status \
  --manifest "${theirstack_manifest}"
```

- `preview` checks the query with blurred, non-credit-consuming results.
- `fetch` is the paid step and refuses to run without an explicit paid-use guard and bounded credit
  limit.
- `apply` replays the cached full response into per-company observation sources without calling the
  vendor again.
- `status` reports cached query, credit, normalization, identity, and apply progress without making
  a paid request.

The default preview is a global remote search rather than a Pakistan country gate. It samples
backend, software, full-stack, production AI, data, frontend, platform, founding, and
description-explicit worldwide lanes. Vendor `remote: true` remains discovery evidence, not proof
that an applicant in every country is eligible. Local selection requires a stable vendor company
ID, keeps one employer per slot before relaxing diversity, and rejects mixed temporary/volunteer,
freelance, management, junior, QA, and other out-of-lane records. Add prior paid IDs with
`--exclude-job-ids-file PATH`; a repeated paid request otherwise consumes credits again.

Responses, query manifests, and checkpoints stay under ignored `data/local/cache/theirstack/` and
`data/local/runs/`; they never contain the API token. When a result provides a native Greenhouse or
Ashby URL, route that URL inventory through `scripts/stage_ingest.py`. A successful native adapter
snapshot can then provide lifecycle-managed confidence, while the original TheirStack assertion is
preserved independently. Staging refuses to attach a shared provider board when one source run
attributes it to multiple local companies; reconcile that identity explicitly instead of choosing
the newest observation.

## Results

Export normalized active jobs:

```bash
uv run python scripts/generate_job_opportunities.py --limit 100
```

The same command writes three separate artifacts instead of treating every lead as ready to
apply:

- `application_queue.{json,csv}` contains fresh target roles with explicit Pakistan or worldwide
  remote evidence and a public job URL.
- `verification_queue.{json,csv}` contains target roles whose geographic eligibility still needs
  manual confirmation.
- `application_pool_summary.json` records queue size, provider contribution, freshness, and
  exclusion reasons.

Generate the full ranked queues rather than a small inspection sample with:

```bash
uv run python scripts/generate_job_opportunities.py \
  --output-dir data/local/runs/current \
  --limit 200000 \
  --queue-limit 500
```

`generate_weekly_targets.py` separately writes `company_outreach_queue.{json,csv}`. Company
outreach is never interpreted as an application queue and neither command submits applications.
Validate the selected public URLs sequentially, then report queue and dead-link metrics:

```bash
uv run python scripts/validate_application_urls.py \
  --queue application_queue=data/local/runs/current/application_queue.json \
  --queue verification_queue=data/local/runs/current/verification_queue.json \
  --output data/local/runs/current/application_url_validations.json \
  --delay-seconds 1

uv run python scripts/report_application_pool.py \
  --run-dir data/local/runs/current \
  --url-validations data/local/runs/current/application_url_validations.json \
  --output data/local/runs/current/application_pool_metrics.json
```

URL validation uses public `GET` requests only, rejects non-public network targets, follows a
bounded number of redirects, honors `Retry-After`, and caches results. Check complete-snapshot
freshness independently of observation feeds with:

```bash
uv run python scripts/check_pipeline_freshness.py \
  --max-age-hours 24 \
  --output data/local/runs/current/pipeline_freshness.json
```

Export only locally classified worldwide-remote IC engineering candidates from TheirStack:

```bash
uv run python scripts/generate_job_opportunities.py \
  --provider theirstack \
  --remote-status global_explicit \
  --role-status strong \
  --role-status possible \
  --limit 200 \
  --output-dir data/local/runs/theirstack/global-review
```

This is a review queue, not work-authorization proof. Keep `remote_unclear` visible in a separate
research export when native job pages may contain better evidence.

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
