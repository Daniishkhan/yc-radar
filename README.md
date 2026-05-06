# YC Radar

YC Radar is a FastAPI workbench for turning the YC company directory into practical senior-engineer entry points: target lists, prototype missions, open-source PR ideas, founder outreach briefs, and eventually autonomous scouting agents.

The first version is intentionally grounded and boring in the right places:

- `data/yc_companies_raw.json` keeps the public YC/Algolia export.
- `data/yc_radar.db` is the local SQLite source of truth for companies, YC jobs, and career surfaces.
- `data/yc_companies_prototype_targets.csv` ranks companies for prototype outreach.
- The API reads local data first, so it works before we wire in databases or queues.
- Playbooks are deterministic by default, then can be refined by an LLM when `OPENAI_API_KEY` is set.

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

The extractor queries the same public Algolia index used by `ycombinator.com/companies`, splits by batch to avoid the 1,000-result cap, enriches hiring companies with YC page-prop job postings, writes SQLite tables, and exports JSON/CSV snapshots into `data/`.

## Discover Career URLs

```bash
uv run python scripts/discover_career_urls.py --limit 20
```

This deterministic resolver reads companies and YC jobs from SQLite, then finds career surfaces using YC job URLs, homepage links, `robots.txt` sitemaps, one-level sitemap indexes, and capped common-path probes. It does not use Firecrawl or browser automation. Results are written to SQLite and exported to:

- `data/yc_career_surfaces_raw.json`
- `data/yc_career_surfaces.csv`

The HTTP cache lives in ignored `data/cache/career_url_discovery.json`.

## One-Time Candidate Knowledge Base

Put the resume PDF at `data/resume/resume.pdf`, then run:

```bash
uv run python scripts/ingest_resume.py
```

This writes:

- `data/profile/resume_text.txt`
- `data/profile/candidate_profile.json`

Those files are intentionally ignored by git because they contain personal information.
They are local inputs for future agent scripts and should not be exposed through the API.

## Weekly Candidate Fit Engine

Generate a local target list from the YC export and your private candidate profile:

```bash
uv run python scripts/generate_weekly_targets.py --limit 40 --candidate-pool 100
```

The script writes ignored, local run artifacts into `data/runs/YYYY-MM-DD/`:

- `weekly_targets.json`
- `weekly_targets.csv`
- `hiring_verifications.json`

Hiring verification treats YC's `isHiring` as `yc_is_hiring`, then optionally checks live pages with Firecrawl. The v1 verifier is free-plan-safe: it scrapes the company homepage, detects likely careers/jobs links, scrapes at most two more exact pages per company, caps concurrency at `2`, and caches results so reruns do not spend duplicate credits. It does not use wildcard crawls or broad domain extraction.

Useful dry runs:

```bash
uv run python scripts/generate_weekly_targets.py --no-verify-hiring --no-llm --limit 5 --candidate-pool 10
uv run python scripts/generate_weekly_targets.py --verify-hiring --no-llm --limit 5 --candidate-pool 10
```

Use the first command when you want zero API spend. Use the second for a tiny live Firecrawl smoke test.

## Useful Endpoints

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

## Direction

The app is meant to grow into a set of agents:

- **Scout Agent:** enriches companies with founders, CTOs, GitHub repos, docs, and careers pages.
- **Prototype Agent:** proposes narrow demos that can be built in a few hours.
- **PR Agent:** finds open-source issues and creates contribution plans.
- **Outreach Agent:** writes concise founder emails with a repo/Loom angle.
- **Portfolio Agent:** tracks shipped artifacts and follow-ups.
