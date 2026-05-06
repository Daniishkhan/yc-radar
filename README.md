# YC Radar

YC Radar is a local workbench for turning YC company data into senior-engineer entry
points: companies to target, jobs worth filtering for, career pages to scrape, prototype
ideas to build, and eventually founder outreach that is backed by something concrete.

The goal is not to build another job board. The goal is to find small, sharp openings where
an AI/backend/data engineer can get noticed by shipping something useful before sending the
email.

## Stack

- Python 3.11+
- FastAPI and Uvicorn for the local API
- Pydantic v2 for settings and response models
- SQLAlchemy with SQLite for local persistence
- httpx for deterministic HTTP fetching
- OpenAI SDK for optional LLM-refined briefs
- Firecrawl SDK for optional live hiring verification
- pypdf for one-time resume ingestion
- uv for dependency management and scripts
- pytest and Ruff for tests and linting
- Docker Compose for a containerized local run

## Data Model

`data/yc_radar.db` is the primary local database.

Core tables:

- `companies`: YC company profile data, prototype score, prototype angle, and raw payload.
- `yc_job_postings`: YC job postings extracted from company page props, including title,
  location, salary, equity, visa, skills, and raw payload.
- `career_page_discovery_events`: raw, non-lossy evidence from YC job URLs, homepage links,
  sitemaps, and capped common-path probes.
- `company_career_pages`: clean, deduped external career/jobs/ATS URLs for each company.

Useful view:

- `company_primary_career_pages`: one best external career URL per company for quick TablePlus
  inspection.

CSV and JSON files in `data/` are snapshots for inspection and debugging. They are useful,
but the app should read from SQLite first.

Ignored local data:

- `data/profile/`: candidate profile extracted from the resume.
- `data/resume/`: private resume PDFs.
- `data/runs/`: weekly target outputs.
- `data/cache/`: HTTP cache for deterministic discovery.

## Quick Start

```bash
uv sync --extra dev
cp .env.example .env
uv run uvicorn yc_radar.main:app --reload
```

Open:

- API: `http://localhost:8000`
- Docs: `http://localhost:8000/docs`
- Top targets: `http://localhost:8000/targets`

## Refresh YC Data

```bash
uv run python extract_yc_companies.py
```

This script queries the public Algolia index used by `ycombinator.com/companies`, splits by
YC batch to avoid the 1,000-result cap, then fetches YC company pages for hiring companies
to extract structured job postings from page props.

It writes:

- `data/yc_radar.db`
- `data/yc_companies_raw.json`
- `data/yc_companies.csv`
- `data/yc_companies_prototype_targets.csv`
- `data/yc_job_postings_raw.json`
- `data/yc_job_postings.csv`

The current checked-in snapshot has 5,880 companies and 4,833 YC job postings.

## Discover Career URLs

```bash
uv run python scripts/discover_career_urls.py --limit 100 --concurrency 10
```

This script reads from SQLite and finds career pages without Firecrawl or browser
automation. It uses a deterministic, cheap path:

- Seed from known YC job posting URLs.
- Fetch each company homepage once and extract career/jobs/ATS links.
- Fetch `robots.txt` and sitemap files with one-level sitemap index expansion.
- If nothing useful is found, probe a small fixed set like `/careers`, `/jobs`, `/join-us`,
  `/join`, `/work-with-us`, and `/open-positions`.

It writes raw observations and clean canonical pages separately:

- `career_page_discovery_events`: raw evidence, including YC job URLs.
- `company_career_pages`: deduped external career/jobs/ATS URLs.
- `company_primary_career_pages`: view with one best URL per company.

It exports:

- `data/company_career_pages_raw.json`
- `data/company_career_pages.csv`
- `data/career_page_discovery_events_raw.json`
- `data/career_page_discovery_events.csv`

The current checked-in sample was run against 100 companies and found 34 canonical external
career/job/ATS URLs from 160 raw discovery events.

Inspect external career URLs:

```bash
uv run python - <<'PY'
import sqlite3

conn = sqlite3.connect("data/yc_radar.db")
for row in conn.execute("""
    SELECT company_slug, company_name, career_page_url, discovery_source, confidence, http_status
    FROM company_career_pages
    ORDER BY confidence DESC, company_slug
"""):
    print(row)
conn.close()
PY
```

Inspect one best URL per company:

```bash
uv run python - <<'PY'
import sqlite3

conn = sqlite3.connect("data/yc_radar.db")
for row in conn.execute("""
    SELECT company_slug, company_name, career_page_url, discovery_source, confidence
    FROM company_primary_career_pages
    ORDER BY company_slug
"""):
    print(row)
conn.close()
PY
```

Run the full directory when you are ready to spend the time and requests:

```bash
uv run python scripts/discover_career_urls.py --concurrency 10
```

## Candidate Knowledge Base

Put the resume PDF at `data/resume/resume.pdf`, then run:

```bash
uv run python scripts/ingest_resume.py
```

This writes:

- `data/profile/resume_text.txt`
- `data/profile/candidate_profile.json`

Those files are ignored by git because they contain private candidate information. Future
agents should use them as local context, not expose them through the API.

## Weekly Candidate Fit Engine

Generate a local shortlist:

```bash
uv run python scripts/generate_weekly_targets.py --limit 40 --candidate-pool 100
```

Useful smoke tests:

```bash
uv run python scripts/generate_weekly_targets.py --no-verify-hiring --no-llm --limit 5 --candidate-pool 10
uv run python scripts/generate_weekly_targets.py --verify-hiring --no-llm --limit 5 --candidate-pool 10
```

The first command uses no paid APIs. The second performs a tiny Firecrawl-backed live hiring
check. Firecrawl usage is intentionally free-plan-safe: exact pages only, no wildcard domain
crawls, at most three pages per company, and cached results in `data/runs/YYYY-MM-DD/`.

## API

Useful endpoints:

- `GET /health`
- `GET /companies?query=agent&hiring=true&max_team_size=10`
- `GET /companies/{slug}`
- `GET /targets?limit=50&hiring=true&max_team_size=10`
- `GET /missions/{slug}`
- `POST /missions/{slug}/brief`

The API uses `CompanyRepository`, which reads from SQLite when `data/yc_radar.db` has company
rows and falls back to CSV snapshots only when the database is empty.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

## Where This Is Going

Near-term useful agents:

- Scout: enrich companies with founders, CTOs, GitHub repos, docs, career pages, and hiring
  signals.
- Fit: rank companies and jobs against the candidate profile, including visa/location
  eligibility.
- Prototype: propose a small demo that can be built in a few hours for a specific company.
- PR: find open-source contribution angles where the company has public repos.
- Outreach: draft concise founder/CTO emails tied to the prototype or PR.

The bias should stay practical: deterministic data first, LLMs second, and every output should
make it easier to take a real shot at one company.
