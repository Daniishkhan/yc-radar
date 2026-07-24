# Source-neutral hiring-radar foundation

YC Radar remains a local, script-first workbench. YC supplies the initial company registry and raw
YC job evidence; it does not define canonical job identity. The first source-neutral vertical slice
adds public Greenhouse boards without a server, queue, browser, Firecrawl, or application flow.

## Source truth and lifecycle

`company_career_pages` and `discovered_urls` are URL evidence. Greenhouse discovery extracts a
validated public board token and registers one `career_sources` row per provider/token. The stable
canonical job key is `(provider, career_source_id, external_job_id)`. URLs remain mutable public
attributes and are never identity.

A `source_sync_runs` row is committed as `running` before every fetch, so an interrupted request
remains auditable. Only an HTTP-200, valid complete board snapshot is applied. It creates/updates
`job_postings` as current state, appends a `job_posting_versions` row only when normalized
user-visible content hash changes, and records body-free `job_posting_observations`. The full public
provider job payload is retained on the immutable version, not repeated in every run. A completed
run key replays without another fetch; failed, partial, or interrupted keys require a new key for a
new attempt. `career_sources.last_synced_at` means last successfully applied complete snapshot;
`last_sync_status` also records failed or partial attempts.

- New job: current active row, first version, and seen observation.
- Unchanged job: advance `last_seen_at`, reset misses, add observation, no duplicate version.
- Changed job: update current hash/fields and append one version.
- First consecutive complete absence: retain active status.
- Second consecutive complete absence: set `closed` and `closed_at`.
- Failed, malformed, partial, or duplicate-ID snapshots: persist run error only; no lifecycle state,
  version, or observation changes.
- Reappearance: reactivate the same row and clear `closed_at`; append a version only if content
  differs.

Current jobs retain source published/updated timestamps plus first/last seen, last changed, status,
content hash, misses, and closed time. Existing YC tables and URL-derived `external_job_postings`
remain source-specific/raw workflows and are intentionally not backfilled into canonical jobs.

## Greenhouse boundary

The supported adapter performs read-only GET requests to
`https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`. It uses a clear local
user agent, 15-second bounded httpx timeout, and at most three retry backoffs for 429, 5xx, and
transient transport errors. It does not use credentials, submit applications, crawl broadly, or
infer remote/visa eligibility. Tests mock all network responses.

## Operations

Fresh database:

```bash
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head
uv run python scripts/load_snapshots.py
uv run python scripts/sync_job_sources.py discover-greenhouse
uv run python scripts/sync_job_sources.py sync --provider greenhouse --limit 5
uv run python scripts/generate_job_opportunities.py --limit 50
```

Existing populated legacy schema:

```bash
# Back up first. This verifier is read-only.
uv run python scripts/migrate_database.py verify-existing
uv run alembic stamp 0001_baseline
uv run alembic upgrade head
uv run alembic current
```

Never stamp a legacy database directly to `head`: that would claim the additive source-neutral
tables exist when they do not. Stop if the verifier reports drift. Destructive migration downgrade
or reset is only for explicitly confirmed disposable/local data.

Weekly targets include active canonical job role evidence and public provenance, but canonical
source observation does not change Firecrawl's separate `verified_hiring_status`. The job-first
export contains public company/job provenance only; it does not read or emit resume, profile, or
contact data from ignored `data/local/` paths.

## Deferred roadmap

Additional ATS adapters, non-YC company onboarding, remote eligibility evidence, hiring-intent
signals, VC/company sources, alerting, multi-worker scheduling, reconciliation with URL-derived
external jobs, and any public product/API are intentionally deferred.
