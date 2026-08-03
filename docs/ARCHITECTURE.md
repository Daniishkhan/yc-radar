# System Design and Operations

YC Radar is a local data pipeline, not a served application. It converts public company and job
source evidence into a durable Postgres inventory, then ranks that inventory for a candidate.

## System Shape

```text
registries / public boards / crawl files / vendor exports
                         |
             direct adapter sync or ingest staging
                         |
company -> company source -> sync run -> canonical jobs
                                      |
                         deterministic classification
                                      |
                         local CSV/JSON shortlist
```

The company is the root identity. YC is one optional directory source; Greenhouse and Ashby are
job-board sources. A future paid vendor is another source or evidence producer, not a replacement
company/job model.

## Canonical Data Model

The public schema contains four application tables:

| Table | Responsibility |
|---|---|
| `companies` | Neutral employer identity, including verified/provisional state |
| `company_sources` | Provider identity, external ID, URL, sync mode, and provider metadata |
| `sync_runs` | Immutable attempt status, completeness, counters, errors, and timestamps |
| `jobs` | Current normalized state keyed by source and provider-owned job ID |

Company ownership has one path: `jobs.company_source_id -> company_sources.company_id`. A job does
not store `company_id` separately. Cross-provider duplicates remain independent source assertions;
read paths may cluster them when strong identity anchors agree.

Alembic migrations are the schema authority. SQLAlchemy definitions in
`src/yc_radar/services/database.py` must match the migration head.

## Source Synchronization

Provider adapters implement the contract in `src/yc_radar/domain/job_sources.py`:

```text
public source -> SourceSnapshot[NormalizedJob] -> JobSyncService
              -> sync_runs + jobs
```

`JobSyncService` is the only canonical job lifecycle writer. A successful complete snapshot marks
seen jobs active. The first complete absence increments a miss; the second consecutive complete
absence closes the job. Failed and partial attempts do not increment misses. Reappearing external
IDs reactivate their existing rows.

Observation-mode sources may add seen evidence but cannot infer absence or close jobs.

## Staging Interface

The `ingest` schema is a durable work area for large, dirty source-URL inventories:

| Table | Responsibility |
|---|---|
| `ingest.runs` | Idempotent input identity, parser versions, cursor, and aggregate state |
| `ingest.raw_observations` | Every producer observation and bounded error/provenance payload |
| `ingest.url_work_items` | Globally deduplicated URL work, stages, leases, retries, and artifacts |
| `ingest.job_candidates` | Typed extracted candidates linked to their raw evidence |

The supported CLI is `scripts/stage_ingest.py`:

```text
load -> fetch -> parse -> enrich -> promote -> done
         |        |        |          |
       cache   provider   company   complete snapshot
       body    + token    identity   into JobSyncService
```

- `load` streams CSV/JSONL batches and resumes by file hash and committed cursor.
- `work` claims one stage using owner/token/expiry leases and `FOR UPDATE SKIP LOCKED`.
- `status` reports run/queue state and the compact discovery-to-shortlist funnel.
- `requeue` recovers expired leases and can explicitly retry quarantined/dead items.
- `promote` refetches the detected board through its adapter and requires a complete valid snapshot
  before registering the source or writing canonical jobs.

Network work happens outside claim transactions. Responses are streamed with size limits into
`DiskHttpCache`; database rows keep hashes and artifact pointers. Missing/malformed URLs and
oversized row payloads are retained as bounded evidence without aborting valid rows. Structural
file errors such as malformed JSON, timestamps, or priorities stop the load at its committed
cursor so the producer can be corrected and the same run resumed.

URL work is globally keyed by normalized URL plus parser/normalizer versions. Multiple runs retain
their own raw observations while reusing completed work. Change a version only when interpretation
logic changes. Staging discovers and first-promotes a source; recurring board refresh belongs to
`scripts/sync_job_sources.py`.

## Identity and Ranking

Identity resolution fails closed. Exact names alone do not justify a merge when domains conflict
or are missing. Verified boards that cannot be safely matched may receive isolated provisional
companies so their jobs are retained without contaminating another employer.

Role and remote classification in `candidate_fit.py` is deterministic. The pipeline ranks every
registered company before truncating the candidate pool. A missing website does not suppress a
company with verified job evidence. Worldwide, Pakistan, regional, restricted, unclear, onsite,
and absent remote evidence remain distinct categories; none imply visa or work authorization.

## Operational Paths

Use direct registration/sync for known boards:

```bash
uv run python scripts/register_job_source.py --company-slug example \
  --source-url https://job-boards.greenhouse.io/example
uv run python scripts/sync_job_sources.py --provider greenhouse --delay-seconds 2
```

Manual source registration is a trusted operator override: it validates the provider URL but does
not fetch a snapshot first. Automated scout and staging promotion require a complete provider
snapshot before registration.

Use staging for large URL inventories. Scope work with both source and run key; unscoped workers
operate on the global queue. Keep network batches within their lease period and inspect `status`
for delayed retries because `claimed: 0` can mean backoff rather than completion:

```bash
uv run python scripts/stage_ingest.py load --input INPUT.jsonl \
  --source SOURCE --run-key RUN_KEY
uv run python scripts/stage_ingest.py work --stage fetch --source SOURCE --run-key RUN_KEY
uv run python scripts/stage_ingest.py work --stage parse --source SOURCE --run-key RUN_KEY
uv run python scripts/stage_ingest.py work --stage enrich --source SOURCE --run-key RUN_KEY
uv run python scripts/stage_ingest.py promote --source SOURCE --run-key RUN_KEY
uv run python scripts/stage_ingest.py status --source SOURCE --run-key RUN_KEY
```

Use a destructive rebuild only when the configured database may be replaced:

```bash
uv run python scripts/reset_database.py --yes --rebuild-schema
uv run python scripts/load_snapshots.py
```

## Extension Boundaries

- New directory: create/update companies and attach a `company_sources` identity.
- New complete ATS/feed: add an adapter and route snapshots through `JobSyncService`.
- New crawl/vendor URL feed: load observations through staging and add deterministic parser support.
- New enrichment: retain raw evidence first; add typed canonical fields only when stable and useful.
- New output: query canonical tables and keep generated files under `data/local/runs/`.

Do not create an API server, provider-specific table family, parallel lifecycle writer, or broad
browser scraper unless the product requirements explicitly change.
