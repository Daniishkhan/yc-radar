# YC Radar

YC Radar is a personal, script-first hiring-radar workbench for finding high-signal companies
and senior backend/software engineering roles. Companies are first-class local entities; YC is one
optional company-directory source, not the seed for career discovery or ATS synchronization. The
job-source registry currently supports public Greenhouse and Ashby boards.

The goal is simple:

1. Find companies that are hiring, quietly hiring, or worth approaching even if they do not have
   an obvious job post.
2. Filter for backend-specific and senior SWE roles where systems work matters.
3. Prioritize companies where my backend, DevOps, data, and AI experience maps to real engineering
   work: system design, infrastructure, performance, caching, debugging, security, and reliability.
4. Write the final shortlist to Postgres and/or CSV files that I can inspect, refine, and act on.
5. Use agents only where they improve the shortlist or outreach play, not as the product surface.

This is not a web app, public API, or generic job board. It is a local intelligence pipeline for
deciding where I should apply and what proof points I should lead with.

## What It Does Today

The current implementation has independent company, company-source, and job-source registries:

- Pulls YC company data from the same public source used by `ycombinator.com/companies`.
- Extracts structured YC job postings, including salary, equity, skills, location, and visa fields.
- Stores the data in local Docker-backed Postgres for inspection in TablePlus.
- Discovers external career pages, jobs pages, and ATS pages from company websites.
- Keeps raw discovery evidence separate from clean deduped career page results and the URL queue.
- Fetches discovered URLs into reusable source documents, then classifies whether each page is a
  career home, job listing, ATS listing, individual job detail, fetch error, or irrelevant page.
- Ingests my resume into a private local profile file.
- Registers standalone companies before any optional YC, directory, or ATS association.
- Detects and syncs configured public Greenhouse and Ashby boards through read-only GET endpoints.
- Tracks
  canonical current jobs, immutable content versions, and per-run observations.
- Generates candidate-fit target lists from every registered company plus optional YC evidence and
  active canonical jobs.
- Writes shortlist outputs to local CSV/JSON files and Postgres-backed tables.

## Where It Is Going

The next version should turn the provider-neutral registry into a practical weekly shortlist
without turning the repo into a service.

- Expand company registries and supported ATS/feed adapters without privileging YC membership.
- Verify live career pages and hidden jobs.
- Use agents to inspect company websites, products, docs, GitHub repos, and job pages when that
  improves the output.
- Score companies against my profile: senior backend/SWE fit, backend-heavy full-stack fit,
  systems/infrastructure depth, DevOps fit, LLM/data systems proof points, remote/global
  eligibility, and team size.
- Return roughly 50 to 100 companies worth actioning in a CSV or DB table.
- For each company, suggest a demo or contribution I can ship in a few hours.
- Help draft founder/CTO outreach tied to the actual artifact.
- Add additional company feeds, such as Apollo or Bright Data, through `company_sources` without
  mixing them into YC-specific tables.

The output should answer: "Which backend/senior SWE companies should I apply to or build for this
week, and why am I a credible fit?"

## Stack

- Python 3.11+
- Postgres with SQLAlchemy, JSONB, full-text search, and pgvector
- Pydantic v2
- httpx for deterministic website checks
- OpenAI SDK for LLM-assisted ranking and outreach
- Firecrawl SDK for optional live hiring verification
- pypdf for resume ingestion
- uv for dependency management
- pytest and Ruff

## Database

The main database is local Postgres, run through Docker Compose:

```bash
docker compose up -d postgres
```

Default connection string:

```text
postgresql+psycopg://yc_radar:yc_radar@localhost:5433/yc_radar
```

The Compose file uses the `pgvector/pgvector:0.8.2-pg17-trixie` image with a named Docker volume
for persistence. It publishes container port `5432` on host port `5433` to avoid colliding with a
machine-level Postgres install.

Important tables:

- `companies`: standalone source-neutral employer identities.
- `company_sources`: optional company-directory identities such as YC or a curated list. ATS board
  identities do not belong here.
- `yc_company_profiles`: YC-only metadata attached to companies that happen to be in YC.
- `yc_job_postings`: YC job posts with title, URL, salary, equity, location, visa, skills, and
  raw payloads.
- `career_page_discovery_events`: raw evidence from YC job URLs, homepage links, sitemaps, and
  common path probes.
- `career_page_discovery_statuses`: per-company checkpoint status for resumable discovery runs.
- `company_career_pages`: clean deduped external career/jobs/ATS URLs.
- `discovered_urls`: URL inventory queued for fetching, classification, extraction, and later
  enrichment.
- `source_documents`: raw and cleaned page/document text for company, job, docs, and enrichment
  sources.
- `page_classifications`: deterministic page-kind labels with JSONB evidence, separating career
  homes, job listings, ATS listings, job details, fetch errors, and irrelevant pages.
- `external_job_postings`: normalized jobs discovered outside YC.
- `job_extraction_runs`: deterministic parser and LLM extraction metadata.
- `document_chunks`: searchable text chunks with generated Postgres full-text vectors.
- `document_embeddings`: pgvector-backed embeddings for semantic retrieval.
- `job_role_signals`: extracted role-fit evidence such as backend, infra, data, seniority, remote,
  and visa signals.
- `career_sources`: independently configured public ATS/feed boards, identified by provider plus
  stable external source ID.
- `source_sync_runs`: persisted fetch status, completeness, counters, and bounded errors.
- `job_postings`: canonical current jobs, uniquely identified by provider + career source + external
  job ID (never by URL).
- `job_posting_versions`: append-only normalized public content snapshots, written only on content
  changes.
- `job_posting_observations`: per-complete-run seen/missed evidence.

Useful view:

- `company_primary_career_pages`: one best external career URL per company.

CSV files in `data/snapshots/` are lightweight inspection exports. Final target runs live under
ignored `data/local/runs/` by default and can be promoted into committed snapshots or DB tables
when useful. Raw JSON debug payloads,
resume/profile data, caches, and run outputs live under ignored `data/local/` paths.
The application reads from Postgres only; there is no SQLite fallback.

Private local data is ignored by git:

- `data/local/resume/`
- `data/local/profile/`
- `data/local/runs/`
- `data/local/cache/`
- `data/local/debug/`

## Source-Neutral Company and Job Lifecycle

Alembic is the schema authority. For a fresh local database, use:

```bash
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head
uv run python scripts/load_snapshots.py
uv run python scripts/sync_job_sources.py discover
uv run python scripts/sync_job_sources.py sync --limit 5 --delay-seconds 2
```

For an existing populated pre-Alembic database that exactly matches the known **0001 baseline**, back
it up, run the verifier, and only then stamp that baseline. Do **not** stamp an older, partial, or
source-neutral unversioned schema: the verifier fails closed, so restore it to a known state or use
the explicitly destructive rebuild path instead.

```bash
uv run python scripts/migrate_database.py verify-existing
uv run alembic stamp 0001_baseline
uv run alembic upgrade head
uv run alembic current
```

`companies` is the root employer registry (local ID, name, stable slug, website, and verified
primary domain). `company_sources` only maps company-directory identities to those employers;
`yc_company_profiles` contains optional YC-only metadata; `career_sources` independently registers
public ATS/feed boards. A company can exist with no company source and no job source.

Register a company independently, then attach any supported job source:

```bash
uv run python scripts/register_company.py \
  --name "Example, Inc." \
  --website https://example.com

uv run python scripts/register_job_source.py \
  --company-slug example-inc \
  --source-url https://job-boards.greenhouse.io/example

uv run python scripts/register_job_source.py \
  --company-slug another-company \
  --source-url https://jobs.ashbyhq.com/another-company
```

Company registration only reuses an exact verified primary-domain and normalized-name match.
Job-source registration detects the provider from the public URL and refuses to move an existing
board identity between companies. Ambiguous identity evidence stops rather than silently merging.

### Bulk Greenhouse scouting

Common Crawl's public URL Index can seed Greenhouse discovery without guessing board tokens. The
query script registers only one requested crawl partition in Athena, searches both US and EU board
hosts, and writes the resulting candidate CSV under ignored `data/local/debug/`:

```bash
aws login --profile radar-athena

uv run python scripts/query_commoncrawl_greenhouse.py \
  --profile radar-athena \
  --workgroup radar-commoncrawl \
  --database radar_commoncrawl
```

The AWS side should use `us-east-1`, a private encrypted S3 results bucket with an expiration
lifecycle, and a dedicated Athena workgroup with an enforced per-query bytes-scanned cutoff. The
script prints the actual bytes scanned and a rough query-cost estimate. It does not download crawl
content; it reads the Parquet URL Index and exports only deduplicated board candidates.

Verify candidates without writing to Postgres first:

```bash
uv run python scripts/scout_greenhouse_sources.py \
  --input data/local/debug/greenhouse_board_candidates_CC-MAIN-2026-30.csv \
  --output data/local/debug/greenhouse_board_verification_CC-MAIN-2026-30.csv
```

Add `--apply` only when registration is intended. The scout is sequential, cached, and resumable.
It accepts a provider-confirmed company name plus either one unique existing company match or a
company-controlled domain from Greenhouse's configured board redirect/logo. Empty, conflicting,
ambiguous, and hosted-board-only identities remain unresolved. New source IDs from the output can
then be synchronized explicitly:

```bash
uv run python scripts/sync_job_sources.py sync \
  --provider greenhouse \
  --min-source-id 123 \
  --delay-seconds 1
```

Greenhouse uses only the unauthenticated public GET endpoint documented by the
[Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html):
`https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`. Requests are
sequential, use a transparent project user-agent plus `Accept: application/json`, respect
`Retry-After`, back off on transient errors, and default to a one-second inter-source delay. The
client does not impersonate a browser and this workbench never submits applications. A sync run is
committed as `running` before its provider request, so an
interrupted fetch remains auditable. A valid complete snapshot adds/updates jobs and records
observations. An unchanged body creates no new version. A job is retained after its first
consecutive complete absence and closes only after its second; failed or partial scans never change
job lifecycle state. `career_sources.last_synced_at` advances only after a complete snapshot is
applied; `last_sync_status` records failed/partial attempts. A returning job reactivates the same
canonical row.

Ashby uses its documented
[public lightweight posting endpoint](https://developers.ashbyhq.com/docs/public-job-posting-api):
`GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}`. Only jobs with public listing
visibility are synchronized. Ashby follows the same sequential pacing, bounded retry, complete
snapshot, and no-application rules as Greenhouse.

Inspect active canonical jobs without profile/contact data:

```bash
uv run python scripts/generate_job_opportunities.py --limit 50
```

## Pipeline Mental Model

The persistence model is intentionally split by confidence and processing stage:

```text
company registry -> career-page evidence -> provider registry -> career_sources
                                                           -> source_sync_runs
                                                           -> job_postings (current state)
                                                           -> job_posting_versions (history)
                                                           -> job_posting_observations (evidence)

URL evidence -> discovered_urls -> source_documents -> page_classifications
             -> external_job_postings
```

`career_page_discovery_events` is raw evidence. It should be allowed to contain duplicate-looking
clues because it explains where a URL came from. `company_career_pages` is the deduped external
career/jobs/ATS result per company. `discovered_urls` is the fetch queue and URL inventory; it is
the table to extend when Apollo, Bright Data, or other sources start contributing URLs.

`source_documents` stores the fetched page text once. `page_classifications` stores deterministic
page-kind evidence in JSONB so later LLM extraction can operate on a stable document instead of
refetching pages. Only pages classified as `job_detail` with a title are promoted into
`external_job_postings` for backend/SWE fit scoring.

## Quick Start

```bash
uv sync --extra dev
cp .env.example .env
docker compose up -d postgres
uv run alembic upgrade head
uv run python scripts/load_snapshots.py
uv run python scripts/discover_career_urls.py --limit 100 --concurrency 10
uv run python scripts/classify_discovered_urls.py --limit 50 --concurrency 10
uv run python scripts/generate_weekly_targets.py --no-verify-hiring --no-llm --limit 5 --candidate-pool 10
```

The smoke run writes to `data/local/runs/<date>/`. That local output is the thing to inspect,
not a server endpoint.

When starting from scratch during MVP schema work, use the destructive rebuild path:

```bash
uv run python scripts/reset_database.py --yes --rebuild-schema
uv run python scripts/load_snapshots.py
uv run python scripts/discover_career_urls.py --limit 200 --concurrency 10 --batch-size 10
uv run python scripts/classify_discovered_urls.py --limit 100 --concurrency 10
```

The checked-in URL inventory is intentionally broader than the classified subset. Run
classification in bounded batches instead of assuming every discovered URL has already been
fetched or understood.

Reset local Postgres when you want a clean rebuild:

```bash
uv run python scripts/reset_database.py --yes
```

Destructively rebuild the local schema through Alembic only when intentionally resetting local data:

```bash
uv run python scripts/reset_database.py --yes --rebuild-schema
```

## Refresh YC Data

To refresh from the live YC source instead of the checked-in CSV snapshots:

```bash
uv run python scripts/extract_yc_companies.py
```

This refreshes companies and YC job postings, then writes Postgres plus snapshot files:

- `data/snapshots/yc_companies.csv`
- `data/snapshots/yc_job_postings.csv`

Raw JSON debug files are not committed. Write them only when needed:

```bash
uv run python scripts/extract_yc_companies.py --write-raw-json
```

Current checked-in YC source snapshot:

- 6,079 YC companies
- 5,342 YC job postings

## Discover Career Pages

```bash
uv run python scripts/discover_career_urls.py --limit 100 --concurrency 10
```

This finds external career/jobs/ATS pages for every registered company without Firecrawl or browser
automation. Use `--source-provider yc` only when intentionally limiting a run to YC-backed
companies. It checks:

- YC job posting URLs as raw evidence.
- Homepage links.
- `robots.txt` sitemap declarations.
- Common sitemap files.
- A small fixed path list like `/careers`, `/jobs`, `/join-us`, and `/work-with-us`.

It writes:

- `career_page_discovery_events`
- `company_career_pages`
- `discovered_urls`
- `company_primary_career_pages`
- `data/snapshots/company_career_pages.csv`
- `data/snapshots/discovered_urls.csv`
- `data/snapshots/career_page_discovery_events.csv`

Current checked-in discovery inventory:

- 27,569 raw discovery events
- 21,560 clean external career/job/ATS URLs
- 21,560 discovered URLs queued for classification

Inspect clean career pages:

```bash
docker compose exec postgres psql -U yc_radar -d yc_radar -c "
    SELECT company_slug, company_name, career_page_url, discovery_source, confidence, http_status
    FROM company_career_pages
    ORDER BY confidence DESC, company_slug;
"
```

Inspect one best URL per company:

```bash
docker compose exec postgres psql -U yc_radar -d yc_radar -c "
    SELECT company_slug, company_name, career_page_url, discovery_source, confidence
    FROM company_primary_career_pages
    ORDER BY company_slug;
"
```

Run every registered company:

```bash
uv run python scripts/discover_career_urls.py --concurrency 10
```

Discovery runs checkpoint after each batch, so rerunning the same command skips companies already
marked completed. `--limit N` is applied after completed companies are excluded. Use `--force` to
reprocess selected companies and bypass cached responses. A failed homepage request (including HTTP
errors, redirect loops, and transport errors) records a failed checkpoint without deleting prior
events, pages, or URL inventory. A no-pending rerun preserves existing snapshot files.

HTTP cache entries now live under `data/local/cache/career_url_discovery/` and
`data/local/cache/page_fetches/`. They are per-URL metadata plus content-addressed bodies written
atomically. On the first cache miss, each previous `*.json` file is streamed once into the new
cache with bounded memory; a file-revision marker prevents repeated full scans. Legacy files remain
read-only and are never deleted automatically. Use `--cache-dir` and `--legacy-cache-path` to
override them.

## Classify Discovered Pages

```bash
uv run python scripts/classify_discovered_urls.py --limit 50 --concurrency 10
```

This fetches exact discovered URLs, stores the raw and cleaned page text in `source_documents`, and
classifies each page into `page_classifications`. Individual job detail pages are also promoted
into `external_job_postings` with a first-pass backend/SWE role-fit label.

It writes:

- `source_documents`
- `page_classifications`
- `external_job_postings` for pages classified as `job_detail`
- `data/snapshots/page_classifications.csv`

A normal classification run selects only unclassified active URLs. Fetch failures are deliberately
not retried implicitly: request errors and HTTP 408/425/429/5xx are recorded as retryable, while
redirect loops, deterministic 4xx responses, and exhausted budgets are terminal. Retry only the
eligible bounded set explicitly:

```bash
uv run python scripts/classify_discovered_urls.py --retry-fetch-errors --max-fetch-attempts 3 --limit 50
```

`--force` reclassifies all active rows and bypasses cache/retry-budget restrictions. Both discovery
and classification can write atomic local `--status-file` artifacts containing selected/processed
counts, cache metrics, and error classes.

Current checked-in classification smoke:

- 73 source documents fetched
- 35 job listing pages
- 23 career home pages
- 9 individual job detail pages
- 3 ATS listing pages
- 3 fetch errors

## Independent Pipeline Branches and URL Cleanup

Known Greenhouse registration/sync does not depend on bulk URL classification. Run the stages
independently, or use the local branch runner after discovery:

```bash
uv run python scripts/run_pipeline.py --discovery-limit 100 --classification-limit 50 --sync-limit 5
```

The runner starts classification and the all-provider `discover -> sync` branch independently. Its
ignored status files under `data/local/runs/` preserve child raw return codes and map SIGKILL to 137
and SIGTERM to 143 instead of reporting a generic `1`. Complete provider snapshots still apply the
canonical lifecycle atomically; failed or partial source scans do not change misses or closures.

URL cleanup is audit-first and must never run concurrently with discovery, classification, or ATS
registration. It never deletes raw `career_page_discovery_events`; duplicate queue rows are
deactivated and linked fetched rows remain referenced. It also canonicalizes safe host/query
variants across career pages, active queue rows, and registered source URLs. Review the generated
ignored artifacts before the guarded apply:

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
audit=data/local/debug/url-cleanup/$stamp
uv run python scripts/cleanup_url_inventory.py --audit-dir "$audit"
# Review manifest.json, before-counts.json, actions.csv, and actions.jsonl.
uv run python scripts/cleanup_url_inventory.py --apply --audit-dir "$audit"
uv run python scripts/cleanup_url_inventory.py --audit-dir "${audit}-post"
```

`--apply` refuses a missing/stale manifest or changed ordered `actions.jsonl` digest, takes an
exclusive database advisory lock while discovery, classification, and ATS registration hold a
shared writer lock, writes complete before-images plus `backup-manifest.json`, uses one transaction,
verifies raw-event/provider lifecycle and active-primary invariants, and refuses page deletion if a
`career_sources.discovered_from_url` reference remains. The built-in policy is intentionally
narrow: canonical query variants plus audited vendor navigation, third-party sitemap fanout, and
confirmed cross-company redirects; it retains valid company ATS boards and generic career listings.

## Candidate Profile

Put the resume PDF here:

```text
data/local/resume/resume.pdf
```

Then run:

```bash
uv run python scripts/ingest_resume.py
```

This writes private local files:

- `data/local/profile/resume_text.txt`
- `data/local/profile/candidate_profile.json`

These files should stay local because they contain personal information.

## Candidate Fit

Generate a shortlist:

```bash
uv run python scripts/generate_weekly_targets.py --limit 40 --candidate-pool 100
```

Cheap smoke test with no paid API calls:

```bash
uv run python scripts/generate_weekly_targets.py --no-verify-hiring --no-llm --limit 5 --candidate-pool 10
```

Small Firecrawl-backed hiring verification test:

```bash
uv run python scripts/generate_weekly_targets.py --verify-hiring --no-llm --limit 5 --candidate-pool 10
```

Firecrawl should stay free-plan-safe for now: exact pages only, no wildcard crawls, at most
three pages per company, low concurrency, and cached results.

The shortlist is intentionally backend/SWE-focused. AI, LLM, data engineering, full-stack, and
DevOps experience are supporting proof points; they should not turn the list into generic AI
engineer, frontend, research, sales, or marketing roles. Strong matches should point toward system
design, infrastructure, performance, caching, production debugging, security, reliability, or
backend platform ownership.

## Deferred Roadmap

The current foundation intentionally defers ATS adapters beyond Greenhouse and Ashby,
remote/visa eligibility inference, hiring-intent signals, more company-list registries, alerting,
multi-worker operation, and any public web product. Location and other source text are preserved
without making eligibility claims. Resume, profile, contact, cache, and run files remain under
ignored `data/local/` paths and are not emitted by canonical-job exports.

## Product Direction

Build toward one concrete workflow where no company directory is privileged:

```text
company registry -> source registries -> live job evidence -> fit score -> shortlist -> outreach
```

The final product should help me decide:

- Which companies are worth applying to directly?
- Which companies are worth approaching even without a public job?
- Which ones are global/remote-friendly enough for me?
- Which roles are real backend, platform, infrastructure, DevOps-adjacent, or senior SWE fits?
- Which companies give me a path toward senior system design and high-scale engineering work?
- What should I build for each company to stand out?
- Who should I send it to?

The best result is not a bigger database. The best result is a short list of companies where I
can credibly apply as a backend/senior SWE candidate, ship something useful, and start a real
conversation.
