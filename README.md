# YC Radar

YC Radar is my personal tool for finding high-signal companies where I should take a real shot
at getting hired as a senior backend or senior software engineer.

Right now the system is YC-first. The plan is to make the full workflow work end to end on YC
data, then add more sources such as Apollo, Bright Data, company lists, and other enrichment
feeds.

The goal is simple:

1. Find companies that are hiring, quietly hiring, or worth approaching even if they do not have
   an obvious job post.
2. Filter for roles that fit my profile, especially remote/global opportunities and visa/location
   constraints.
3. Surface a small list of high-signal companies.
4. For each target, build a small useful demo, feature, search tool, workflow, or PR.
5. Send that demo to a founder, CTO, or small team as the actual application.

This is not meant to be a generic job board. The playbook is to find companies where a shipped
artifact can get attention faster than a normal application.

## What It Does Today

The current implementation uses YC as the first data source:

- Pulls YC company data from the same public source used by `ycombinator.com/companies`.
- Extracts structured YC job postings, including salary, equity, skills, location, and visa fields.
- Stores the data in a local SQLite database for inspection in TablePlus.
- Discovers external career pages, jobs pages, and ATS pages from company websites.
- Keeps raw discovery evidence separate from clean deduped career page results.
- Ingests my resume into a private local profile file.
- Generates early candidate-fit target lists from YC data and my profile.
- Runs a local FastAPI API for browsing companies, targets, and prototype missions.

## Where It Is Going

The next version should turn YC data into a practical weekly shortlist. After that, the same
workflow should support non-YC sources.

- Search all YC companies, not only the ones YC marks as hiring.
- Verify live career pages and hidden jobs.
- Use AI/browser automation to inspect company websites, products, docs, GitHub repos, and job pages.
- Score companies against my profile: senior backend/SWE fit, backend-heavy full-stack fit,
  LLM/data systems proof points, remote/global eligibility, and team size.
- Return roughly 50 to 100 companies worth actioning.
- For each company, suggest a demo or contribution I can ship in a few hours.
- Help draft founder/CTO outreach tied to the actual artifact.
- Add additional source feeds, such as Apollo or Bright Data, without mixing them into the raw YC
  tables.

The output should answer: "Which companies should I build something for this week?"

## Stack

- Python 3.11+
- FastAPI and Uvicorn
- SQLite with SQLAlchemy
- Pydantic v2
- httpx for deterministic website checks
- OpenAI SDK for LLM-assisted ranking and outreach
- Firecrawl SDK for optional live hiring verification
- pypdf for resume ingestion
- uv for dependency management
- pytest and Ruff
- Docker Compose

## Database

The main database is:

```text
data/yc_radar.db
```

Important tables:

- `companies`: YC company profiles and raw YC payloads.
- `yc_job_postings`: YC job posts with title, URL, salary, equity, location, visa, skills, and
  raw payloads.
- `career_page_discovery_events`: raw evidence from YC job URLs, homepage links, sitemaps, and
  common path probes.
- `company_career_pages`: clean deduped external career/jobs/ATS URLs.

Useful view:

- `company_primary_career_pages`: one best external career URL per company.

CSV and JSON files in `data/` are snapshots for quick inspection. The application should read
from SQLite first.

Private local data is ignored by git:

- `data/resume/`
- `data/profile/`
- `data/runs/`
- `data/cache/`

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

This refreshes companies and YC job postings, then writes SQLite plus snapshot files:

- `data/yc_radar.db`
- `data/yc_companies_raw.json`
- `data/yc_companies.csv`
- `data/yc_companies_prototype_targets.csv`
- `data/yc_job_postings_raw.json`
- `data/yc_job_postings.csv`

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
- `company_primary_career_pages`
- `data/company_career_pages.csv`
- `data/career_page_discovery_events.csv`

Current checked-in sample:

- 100 companies checked
- 160 raw discovery events
- 34 clean external career/job/ATS URLs

Inspect clean career pages:

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

Run the full YC directory:

```bash
uv run python scripts/discover_career_urls.py --concurrency 10
```

## Candidate Profile

Put the resume PDF here:

```text
data/resume/resume.pdf
```

Then run:

```bash
uv run python scripts/ingest_resume.py
```

This writes private local files:

- `data/profile/resume_text.txt`
- `data/profile/candidate_profile.json`

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

The shortlist is intentionally backend/SWE-focused. AI, LLM, data engineering, and full-stack
experience are supporting proof points; they should not turn the list into generic AI engineer,
frontend, research, sales, or marketing roles.

## API

Useful endpoints:

- `GET /health`
- `GET /companies?query=agent&hiring=true&max_team_size=10`
- `GET /companies/{slug}`
- `GET /targets?limit=50&hiring=true&max_team_size=10`
- `GET /missions/{slug}`
- `POST /missions/{slug}/brief`

## Docker

```bash
cp .env.example .env
docker compose up --build
```

## Product Direction

Build toward one concrete workflow, starting with YC and expanding to other company sources later:

```text
company source -> live company/job verification -> profile fit score -> shortlist -> demo idea -> outreach
```

The final product should help me decide:

- Which companies are worth applying to directly?
- Which companies are worth approaching even without a public job?
- Which ones are global/remote-friendly enough for me?
- What should I build for each company to stand out?
- Who should I send it to?

The best result is not a bigger database. The best result is a short list of companies where I
can ship something useful and start a real conversation.
