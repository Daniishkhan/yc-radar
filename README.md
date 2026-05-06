# YC Radar

YC Radar is my personal, script-first workbench for finding high-signal companies where I should
take a real shot at senior backend or senior software engineering roles.

Right now the system is YC-first. The plan is to make the full workflow work end to end on YC
data, then add more sources such as Apollo, Bright Data, company lists, and other enrichment
feeds.

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

The current implementation uses YC as the first data source:

- Pulls YC company data from the same public source used by `ycombinator.com/companies`.
- Extracts structured YC job postings, including salary, equity, skills, location, and visa fields.
- Stores the data in local Docker-backed Postgres for inspection in TablePlus.
- Discovers external career pages, jobs pages, and ATS pages from company websites.
- Keeps raw discovery evidence separate from clean deduped career page results and the URL queue.
- Fetches discovered URLs into reusable source documents, then classifies whether each page is a
  career home, job listing, ATS listing, individual job detail, fetch error, or irrelevant page.
- Ingests my resume into a private local profile file.
- Generates early candidate-fit target lists from YC data and my profile.
- Writes shortlist outputs to local CSV/JSON files and Postgres-backed tables.

## Where It Is Going

The next version should turn YC data into a practical weekly shortlist. After that, the same
workflow should support non-YC sources without turning the repo into a service.

- Search all YC companies, not only the ones YC marks as hiring.
- Verify live career pages and hidden jobs.
- Use agents to inspect company websites, products, docs, GitHub repos, and job pages when that
  improves the output.
- Score companies against my profile: senior backend/SWE fit, backend-heavy full-stack fit,
  systems/infrastructure depth, DevOps fit, LLM/data systems proof points, remote/global
  eligibility, and team size.
- Return roughly 50 to 100 companies worth actioning in a CSV or DB table.
- For each company, suggest a demo or contribution I can ship in a few hours.
- Help draft founder/CTO outreach tied to the actual artifact.
- Add additional source feeds, such as Apollo or Bright Data, without mixing them into the raw YC
  tables.

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

- `companies`: YC company profiles and raw YC payloads.
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

## Pipeline Mental Model

The persistence model is intentionally split by confidence and processing stage:

```text
companies + yc_job_postings
  -> career_page_discovery_events
  -> company_career_pages
  -> discovered_urls
  -> source_documents
  -> page_classifications
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

Expected current smoke shape: 200 companies produce 278 discovery events, 73 deduped discovered
URLs, 73 fetched source documents/classifications, and 9 external job detail postings.

Reset local Postgres when you want a clean rebuild:

```bash
uv run python scripts/reset_database.py --yes
```

Rebuild the local schema when table definitions are changing during MVP iteration:

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

Current checked-in snapshot:

- 5,880 YC companies
- 4,833 YC job postings

## Discover Career Pages

```bash
uv run python scripts/discover_career_urls.py --limit 100 --concurrency 10
```

This finds external career/jobs/ATS pages without Firecrawl or browser automation. It checks:

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

Current checked-in sample:

- 200 companies checked
- 278 raw discovery events
- 73 clean external career/job/ATS URLs
- 73 discovered URLs queued for classification

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

Run the full YC directory:

```bash
uv run python scripts/discover_career_urls.py --concurrency 10
```

Discovery runs checkpoint after each batch, so rerunning the same command skips companies already
marked completed. Use `--force` to reprocess the selected companies.

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

Current checked-in classification smoke:

- 73 source documents fetched
- 35 job listing pages
- 23 career home pages
- 9 individual job detail pages
- 3 ATS listing pages
- 3 fetch errors

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

## Product Direction

Build toward one concrete workflow, starting with YC and expanding to other company sources later:

```text
company source -> live company/job verification -> fit score -> shortlist table/CSV -> demo idea -> outreach
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
