# YC Radar Agent Guide

YC Radar is a local, script-first pipeline for discovering company job sources, synchronizing
public openings, and producing a candidate-specific engineering shortlist. Postgres is the source
of truth. Scripts and local CSV/JSON files are the interface; there is no web service.

Keep this file small. Read [System Design and Operations](docs/ARCHITECTURE.md) before changing the
schema, staging workflow, identity rules, or job lifecycle. Read the
[Engineering Constitution](docs/CONSTITUTION.md) before adding a provider or a new processing
layer.

## Operating Model

```text
company -> company source -> normalized jobs + sync runs -> ranking/export

URL/file evidence -> ingest staging -> verified complete source snapshot
                  -> company source -> normalized jobs
```

The canonical tables are `companies`, `company_sources`, `jobs`, and `sync_runs`. The `ingest`
schema is a bounded work area with `runs`, `raw_observations`, `url_work_items`, and
`job_candidates`; it is not a second company or job model.

`scripts/stage_ingest.py` is the operator-facing staging interface, not an HTTP API. Its commands
are `load`, `work`, `status`, `requeue`, and `promote`; the Python interface lives in
`services/staging.py`. Work advances through `fetch -> parse -> enrich -> promote`. Promotion is
allowed only after a provider adapter returns a valid complete snapshot. Recurring refreshes of
registered Greenhouse and Ashby sources use `scripts/sync_job_sources.py`, not staging.

## Non-Negotiable Rules

- A company is the neutral employer identity. Provider identities and board tokens belong in
  `company_sources`.
- A canonical job is owned by `(company_source_id, external_job_id)`. Never add `company_id` back
  to `jobs` or create provider-specific job tables.
- Do not silently merge ambiguous companies. Keep a verified source on a provisional company until
  independent identity evidence exists.
- Preserve cross-provider job assertions. Deduplicate or cluster only in read/ranking output and
  only with strong anchors.
- Only successful complete snapshots may increment misses or close jobs. Partial/failed runs never
  close jobs; a reappearing provider ID reactivates its row.
- Ingest broadly, classify later. Unknown remote eligibility remains visible for verification and
  is never presented as worldwide or work-authorization evidence.
- Preserve provenance for noisy inputs. Staging imports must be idempotent, resumable, bounded, and
  able to quarantine bad rows without blocking good rows.
- Keep network behavior public, read-only, sequential, and polite. Honor `Retry-After`; do not
  submit applications, spoof browser identity, or introduce browser automation for bulk discovery.
- Keep LLM and Firecrawl behavior optional. Deterministic services remain the default path.
- Keep resume/profile material in ignored `data/local/` paths and never expose it in generated
  output unless explicitly requested.

## Code Map

- `src/yc_radar/domain/job_sources.py`: `SourceSnapshot` and `NormalizedJob` contracts.
- `src/yc_radar/adapters/`: public provider adapters.
- `src/yc_radar/services/database.py`: SQLAlchemy schema definitions.
- `src/yc_radar/services/company_registry.py`: verified/provisional company identity.
- `src/yc_radar/services/job_source_registry.py`: source detection and registration.
- `src/yc_radar/services/job_sync_service.py`: the only canonical job lifecycle writer.
- `src/yc_radar/services/staging.py`: durable import queue and promotion workflow.
- `src/yc_radar/services/candidate_fit.py`: deterministic role and remote classification.
- `scripts/`: supported operator entry points.
- `migrations/`: schema authority.

## Standard Commands

```bash
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head
uv run python scripts/load_snapshots.py

uv run python scripts/sync_job_sources.py --delay-seconds 2
uv run python scripts/stage_ingest.py status
uv run python scripts/generate_job_opportunities.py --limit 100
uv run python scripts/generate_weekly_targets.py --no-verify-hiring --no-llm --limit 20
```

Before handing off code or schema changes:

```bash
uv run pytest
uv run alembic check
uv run ruff check src tests scripts migrations
git diff --check
```

Use `uv`, SQLAlchemy, mocked network tests, and short script entry points. Do not add pip workflows,
SQLite compatibility, API servers, or external exposure unless explicitly requested.
