# YC Radar Agent Notes

This repo is a local, script-first intelligence workbench for YC outreach. Treat it as a practical
pipeline, not a web service template. The useful loop is:

1. Ingest YC companies and jobs.
2. Store the structured data in Docker-backed Postgres.
3. Discover career pages cheaply and deterministically.
4. Fetch and classify discovered URLs into source documents and page kinds.
5. Promote individual job detail pages into normalized external jobs.
6. Rank companies/jobs against the candidate profile.
7. Use deterministic logic and optional agents to refine a backend/SWE shortlist.
8. Write the final output as a CSV and/or Postgres table.

## Project Shape

- `src/yc_radar/domain/models.py`: Pydantic domain models.
- `src/yc_radar/services/`: data access, candidate fit, profile loading, hiring verification.
- `src/yc_radar/playbooks/`: deterministic mission and outreach playbook logic.
- `src/yc_radar/agents/`: OpenAI-assisted refinements; keep LLM usage optional.
- `scripts/extract_yc_companies.py`: refreshes YC company and job data into Postgres plus snapshots.
- `scripts/load_snapshots.py`: seeds Postgres from checked-in YC CSV snapshots.
- `scripts/reset_database.py`: truncates all Postgres tables after explicit `--yes`.
- `scripts/discover_career_urls.py`: finds career/job/ATS URLs without Firecrawl or browser automation.
- `scripts/classify_discovered_urls.py`: fetches discovered URLs, stores source documents, and
  classifies pages as career homes, job listings, ATS listings, or job details.
- `scripts/generate_weekly_targets.py`: creates local candidate-fit target runs.
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

- `companies`
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

Useful view:

- `company_primary_career_pages`: one best external career URL per company.

## Current Pipeline Context

The important design choice is that career discovery and page understanding are separate. Do not
put fetched page text or job-detail assumptions into `company_career_pages`.

Use this mental model:

```text
raw clues -> clean career URLs -> discovered URL inventory -> fetched source document
    -> page classification -> external job posting, only for individual job pages
```

`career_page_discovery_events` can be noisy because it preserves evidence. `discovered_urls` is the
queue that future sources such as Apollo or Bright Data should feed. `source_documents` is the
stable text layer for full-text search, pgvector chunks, deterministic parsers, and later LLM
cleanup. `page_classifications.evidence` is JSONB on purpose; add structured parser evidence there
before reaching for a new table.

Current smoke after a schema rebuild and 200-company discovery:

- 278 discovery events
- 73 `company_career_pages`
- 73 `discovered_urls`
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
uv run python scripts/load_snapshots.py
uv run pytest
uv run ruff check src tests scripts
uv run python scripts/extract_yc_companies.py
uv run python scripts/discover_career_urls.py --limit 100 --concurrency 10
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
uv run ruff check src tests scripts
```

For docs-only changes, a pytest smoke run is usually enough, but lint/test is still preferred
when generated data or scripts changed.
