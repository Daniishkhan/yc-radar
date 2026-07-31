# YC Radar Agent Notes

This repo is a local, script-first hiring-radar workbench. Treat it as a practical pipeline, not
a web service template. Companies are first-class local entities. YC is one optional company
registry and raw evidence source; it must never gate career discovery or ATS synchronization. The
useful loop is:

1. Register neutral companies directly or ingest them from independent company registries.
2. Attach optional company-directory identities such as YC through `company_sources`.
3. Discover public career pages for every company cheaply and deterministically.
4. Register supported job sources through provider adapters (currently Greenhouse and Ashby).
5. Fetch complete provider snapshots sequentially and politely.
6. Preserve canonical current jobs, immutable content history, and per-run observations in
   Docker-backed Postgres.
7. Fetch/classify URL evidence separately from source-board synchronization.
8. Rank active jobs and companies against the candidate profile.
9. Use deterministic logic and optional agents to refine a backend/SWE shortlist.
10. Write local CSV/JSON and/or Postgres outputs.

## Project Shape

- `src/yc_radar/domain/models.py` and `domain/job_sources.py`: Pydantic domain models.
- `scripts/register_company.py`: creates a verified company with no required source membership.
- `scripts/register_job_source.py`: attaches a supported public ATS/feed to an existing company.
- `src/yc_radar/adapters/`: read-only job-source contracts and provider adapters.
- `src/yc_radar/services/company_registry.py`: standalone companies and directory identities.
- `src/yc_radar/services/job_source_registry.py`: provider catalog and persistent job-source registry.
- `src/yc_radar/services/`: data access, lifecycle, candidate fit, profile loading, and hiring verification.
- `migrations/`: Alembic schema history; it is the schema authority.
- `src/yc_radar/playbooks/`: deterministic mission and outreach playbook logic.
- `src/yc_radar/agents/`: OpenAI-assisted refinements; keep LLM usage optional.
- `scripts/extract_yc_companies.py`: refreshes YC company and job data into Postgres plus snapshots.
- `scripts/load_snapshots.py`: seeds Postgres from checked-in YC CSV snapshots.
- `scripts/reset_database.py`: truncates all Postgres tables after explicit `--yes`.
- `scripts/discover_career_urls.py`: finds career/job/ATS URLs without Firecrawl or browser automation.
- `scripts/classify_discovered_urls.py`: fetches discovered URLs, stores source documents, and
  classifies pages as career homes, job listings, ATS listings, or job details.
- `scripts/sync_job_sources.py`: discovers and syncs all configured provider sources.
- `scripts/query_commoncrawl_greenhouse.py`: exports public Greenhouse board candidates from one
  bounded Common Crawl URL Index partition through Athena.
- `scripts/scout_greenhouse_sources.py`: sequentially verifies, resolves, and optionally registers
  Common Crawl Greenhouse candidates with cached, fail-closed identity checks.
- `scripts/generate_weekly_targets.py`: creates local candidate-fit target runs.
- `scripts/generate_job_opportunities.py`: exports public canonical job opportunities locally.
- `scripts/ingest_resume.py`: converts the private resume PDF into local structured profile data.
- `tests/`: deterministic tests; no network calls.

## Product Focus

The current role lane is senior backend / senior software engineering. Prioritize companies and
roles where the work maps to system design, infrastructure, performance, caching, production
debugging, security, reliability, DevOps-adjacent ownership, and backend platform work.

AI, LLM, data engineering, full-stack, and DevOps experience are supporting strengths. They should
make the backend/SWE case sharper, not turn the shortlist into generic AI engineer, frontend-only,
research-only, sales, marketing, or intern roles.

No external service consumes this repo. Do not add API endpoints, web servers, Docker service
flows, or public exposure unless the user explicitly asks for a served interface. Prefer scripts,
small orchestrators, DB tables, and CSV outputs.

## Source Of Truth

Docker-backed Postgres is primary. CSV files in `data/snapshots/` are lightweight inspection
exports. Raw JSON debug artifacts belong under ignored `data/local/debug/`.

Important tables:

- `companies`: source-neutral employer identities (local ID, name, website/domain, local slug).
- `company_sources`: optional company-directory identities mapped to companies. Do not put ATS board
  tokens here.
- `yc_company_profiles`: YC-only profile metadata, separate from neutral employer identity.
- `yc_job_postings`
- `career_page_discovery_events`: raw evidence, including YC job URL observations.
- `career_page_discovery_statuses`: per-company checkpoint status for resumable discovery runs.
- `company_career_pages`: clean deduped external career/jobs/ATS URLs.
- `discovered_urls`: URL inventory queued for fetch, classification, extraction, and future
  enrichment.
- `source_documents`: raw and cleaned source text for company, career, job, docs, and enrichment
  pages.
- `page_classifications`: deterministic page-kind labels and JSONB evidence for fetched URLs.
- `external_job_postings`: normalized non-YC jobs extracted from career/ATS pages.
- `job_extraction_runs`: deterministic parser and LLM extraction metadata.
- `document_chunks`: text chunks with generated Postgres full-text vectors.
- `document_embeddings`: pgvector-backed semantic embeddings.
- `job_role_signals`: extracted role-fit evidence.
- `career_sources`: ATS/feed registrations with stable provider-owned source IDs, independent from
  `company_sources`.
- `source_sync_runs`: immutable sync audit status, counters, completeness, and bounded errors.
- `job_postings`: canonical current state identified by provider + career source + external job ID.
- `job_posting_versions`: immutable public content history, inserted only on content change.
- `job_posting_observations`: seen/missed evidence per successfully applied complete run.

Useful view:

- `company_primary_career_pages`: one best external career URL per company.

## Current Pipeline Context

The important design choice is that career discovery and page understanding are separate. Do not
put fetched page text or job-detail assumptions into `company_career_pages`.

Use this mental model:

```text
raw clues -> clean career URLs -> discovered URL inventory -> fetched source document
    -> page classification -> URL-derived external job posting, only for individual job pages

neutral company -> public career URLs -> provider registry -> career source
    -> complete sync run -> canonical current job
    -> immutable content version on change + per-run seen/missed observation
```

`companies` must not receive source-specific attributes and may exist without any source row. Put
YC-only fields in `yc_company_profiles`. Put YC, curated-list, and future directory identities in
`company_sources`. Put Greenhouse, Ashby, and future ATS/feed identities only in `career_sources`.
Register companies and job sources separately. Ambiguous identity evidence must stop instead of
merging.

`career_page_discovery_events` can be noisy because it preserves evidence. `discovered_urls` is the
queue that future sources such as Apollo or Bright Data should feed. `source_documents` is the
stable text layer for full-text search, pgvector chunks, deterministic parsers, and later LLM
cleanup. `page_classifications.evidence` is JSONB on purpose; add structured parser evidence there
before reaching for a new table. Do not treat URL-derived `external_job_postings` as canonical ATS
jobs: canonical identity requires provider + board + external job ID, not a mutable URL.

Only complete, valid provider snapshots can apply lifecycle changes. First complete absence retains
an active job; second consecutive complete absence closes it. Failed or partial scans must never
increment misses or close jobs. A reappearing ID reactivates the same row. Greenhouse and Ashby use
only their documented unauthenticated public GET endpoints. Keep source fetches sequential and
politely paced, send a transparent project user-agent plus JSON accept header, honor `Retry-After`,
and never submit applications, spoof browser identity, or use browser automation. Ashby sync must
exclude postings whose public `isListed` flag is false.

Weekly target ranking must consider every registered company before truncating the candidate pool.
Current canonical backend/SWE openings should outweigh directory metadata. Remote labels must stay
conservative and deterministic: distinguish explicit worldwide or Pakistan/APAC eligibility from
unclear and geographically restricted remote language, and never present them as work-authorization
or visa conclusions.

Current checked-in discovery inventory:

- 27,569 discovery events
- 21,560 `company_career_pages`
- 21,560 `discovered_urls`
- 73 `source_documents`
- 73 `page_classifications`
- 9 promoted `external_job_postings`

Personal candidate data is ignored and should stay local:

- `data/local/resume/`
- `data/local/profile/`
- `data/local/runs/`
- `data/local/cache/`
- `data/local/debug/`

Do not expose resume/profile contents through any generated output unless the user explicitly asks
for that.

## Commands

```bash
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head
uv run alembic check
uv run python scripts/load_snapshots.py
uv run pytest
uv run ruff check src tests scripts migrations
uv run python scripts/extract_yc_companies.py
uv run python scripts/discover_career_urls.py --limit 100 --concurrency 10
uv run python scripts/sync_job_sources.py discover
uv run python scripts/sync_job_sources.py sync --limit 5 --delay-seconds 2
uv run python scripts/classify_discovered_urls.py --limit 50 --concurrency 10
uv run python scripts/generate_weekly_targets.py --no-verify-hiring --no-llm --limit 5 --candidate-pool 10
```

Clean MVP rebuild/smoke sequence:

```bash
uv run python scripts/reset_database.py --yes --rebuild-schema
uv run python scripts/load_snapshots.py
uv run python scripts/discover_career_urls.py --limit 200 --concurrency 10 --batch-size 10
uv run python scripts/classify_discovered_urls.py --limit 100 --concurrency 10
```

Use `uv`; do not add pip workflows back into the docs.

For an existing populated schema created before Alembic that exactly matches the known 0001
baseline, back up first and verify before adoption. Older, partial, and source-neutral unversioned
schemas must fail closed and must not be stamped:

```bash
uv run python scripts/migrate_database.py verify-existing
uv run alembic stamp 0001_baseline
uv run alembic upgrade head
uv run alembic current
```

Never stamp an existing schema directly to `head`; stop on verifier drift. Fresh databases use
`uv run alembic upgrade head`. Further roadmap work—additional provider adapters, visa eligibility
claims, richer hiring-intent signals, VC/company sources, and any public product—is explicitly
deferred.

## Implementation Preferences

- Keep deterministic logic in services/scripts/playbooks.
- Keep OpenAI-dependent behavior in `agents/` and behind explicit flags like `use_llm`.
- Use SQLAlchemy for DB writes/reads; do not add SQLite compatibility paths.
- Use script entrypoints for workflows. Final outputs should be local CSV files and/or Postgres
  tables, not API responses.
- Keep network-heavy scripts resumable or cached where possible.
- Use table and column names that are obvious in TablePlus. Prefer names like
  `company_career_pages` over abstract names like `surfaces`.
- Tests should mock network behavior.
- Generated YC CSV snapshots can be committed when the user asks to refresh or inspect data.

## Scraping And Enrichment Rules

Career URL discovery should stay cheap:

- No Firecrawl.
- No dynamic browser scraping.
- No broad domain crawls.
- Fetch homepage, `robots.txt`, capped sitemaps, and a small fixed probe list.
- Cache HTTP responses in `data/local/cache/career_url_discovery.json`.

Firecrawl belongs in live hiring verification, not bulk discovery. Keep it free-plan-safe:
exact pages only, at most three pages per company, low concurrency, and cached results.

## Commit Style

Use short Conventional Commit-style subjects when possible:

- `feat: move persistence to postgres`
- `docs: refresh project guide`
- `fix: preserve visa fields in yc job ingestion`

Before committing code changes, run:

```bash
uv run pytest
uv run alembic check
uv run ruff check src tests scripts migrations
```

For docs-only changes, a pytest smoke run is usually enough, but lint/test is still preferred
when generated data or scripts changed.
