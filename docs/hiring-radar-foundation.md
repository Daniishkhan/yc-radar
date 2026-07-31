# Source-neutral hiring-radar foundation

YC Radar remains a local, script-first workbench. Companies are standalone local entities. YC is an
optional company-directory registry and raw job evidence source; it neither seeds nor gates career
discovery. Independent job-source adapters currently support public Greenhouse and Ashby boards
without a server, queue, browser, Firecrawl, or application flow.

## Source truth and lifecycle

`companies` is the source-neutral employer registry and does not require a source row.
`company_sources` maps optional company-directory identities to employers, while
`yc_company_profiles` stores YC-only values. `career_sources` independently stores public ATS/feed
boards. Greenhouse and Ashby IDs belong only in `career_sources`; they are not company-directory
identities. Exact primary-domain plus normalized-name matches may reuse an employer, and ambiguous
name/domain evidence stops rather than merging.

`company_career_pages` and `discovered_urls` are URL evidence. The provider registry detects a
supported board, extracts its stable external source ID, and registers one `career_sources` row per
provider/source pair. The stable canonical job key is
`(provider, career_source_id, external_job_id)`. URLs remain mutable public attributes and are never
identity.

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

## Provider boundaries

The Greenhouse adapter performs read-only GET requests to
`https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`. It uses a transparent
project user-agent, requests JSON explicitly, fetches boards sequentially with a one-second default
inter-source delay, honors `Retry-After`, and uses a 15-second bounded timeout with at most three
retry backoffs for 429, 5xx, and transient transport errors. It does not impersonate a browser, use
credentials, submit applications, crawl broadly, or infer remote/visa eligibility. Tests mock all
network responses.

The Ashby adapter performs read-only GET requests to the documented
[public lightweight endpoint](https://developers.ashbyhq.com/docs/public-job-posting-api) at
`https://api.ashbyhq.com/posting-api/job-board/{job_board_name}`. It requests public compensation
data, stores it in the immutable raw payload, and excludes jobs whose public `isListed` flag is
false. It follows the same pacing, retry, completeness, and no-application rules as Greenhouse.

## Operations

Fresh database:

```bash
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head  # includes independent company and job-source registries
uv run python scripts/load_snapshots.py
uv run python scripts/sync_job_sources.py discover
uv run python scripts/sync_job_sources.py sync --limit 5 --delay-seconds 2
uv run python scripts/generate_job_opportunities.py --limit 50
```

Standalone company plus independent job-source registration:

```bash
uv run python scripts/register_company.py \
  --name "Example, Inc." \
  --website https://example.com

uv run python scripts/register_job_source.py \
  --company-slug example-inc \
  --source-url https://job-boards.greenhouse.io/example
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
tables exist when they do not. `verify-existing` accepts only the exact unversioned 0001 baseline;
older, partial, and source-neutral unversioned schemas must not be stamped. Stop if the verifier
reports drift. Destructive migration downgrade or reset is only for explicitly confirmed
disposable/local data.

Weekly targets include active canonical job role evidence and public provenance, but canonical
source observation does not change Firecrawl's separate `verified_hiring_status`. The job-first
export contains public company/job provenance only; it does not read or emit resume, profile, or
contact data from ignored `data/local/` paths.

Discovery is the shared prerequisite, but URL classification is not a prerequisite for registering
or syncing known Greenhouse boards. `uv run python scripts/run_pipeline.py` launches
classification beside the sequential all-provider `discover -> sync` branch and writes ignored local
stage artifacts with raw child return codes/signals. It does not alter the complete-snapshot
transaction boundary above.

The URL-inventory cleanup is intentionally separate and dry-run by default:

```bash
uv run python scripts/cleanup_url_inventory.py --audit-dir data/local/debug/url-cleanup/<timestamp>
# Review the manifest and actions, then explicitly apply the same reviewed directory.
uv run python scripts/cleanup_url_inventory.py --apply --audit-dir data/local/debug/url-cleanup/<timestamp>
```

The apply path takes an exclusive Postgres advisory lock while discovery, classification, and ATS
registration hold a shared writer lock, binds apply to the reviewed ordered action digest, writes
complete before-images and a backup digest under ignored `data/local/debug`, canonicalizes safe URL
variants, deactivates queue losers rather than deleting them, and never deletes raw discovery
events.

## Deferred roadmap

ATS adapters beyond Greenhouse and Ashby, remote eligibility evidence, hiring-intent signals,
additional company registries, alerting, multi-worker scheduling, reconciliation with URL-derived
external jobs, and any public product/API are intentionally deferred.
