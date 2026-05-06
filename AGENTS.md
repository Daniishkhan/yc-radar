# YC Radar Agent Notes

This repo is a local intelligence workbench for YC outreach. Treat it as a practical system,
not a generic FastAPI template. The useful loop is:

1. Ingest YC companies and jobs.
2. Store the structured data in SQLite.
3. Discover career pages cheaply and deterministically.
4. Rank companies/jobs against the candidate profile.
5. Generate prototype, PR, or founder outreach plays.

## Project Shape

- `src/yc_radar/main.py`: FastAPI app entrypoint.
- `src/yc_radar/api/routes.py`: API endpoints.
- `src/yc_radar/domain/models.py`: Pydantic API/domain models.
- `src/yc_radar/services/`: data access, candidate fit, profile loading, hiring verification.
- `src/yc_radar/playbooks/`: deterministic mission and outreach playbook logic.
- `src/yc_radar/agents/`: OpenAI-assisted refinements; keep LLM usage optional.
- `extract_yc_companies.py`: refreshes YC company and job data into SQLite plus snapshots.
- `scripts/discover_career_urls.py`: finds career/job/ATS URLs without Firecrawl or browser automation.
- `scripts/generate_weekly_targets.py`: creates local candidate-fit target runs.
- `scripts/ingest_resume.py`: converts the private resume PDF into local structured profile data.
- `tests/`: deterministic tests; no network calls.

## Source Of Truth

`data/yc_radar.db` is primary. CSV/JSON files in `data/` are inspection snapshots and debug
artifacts.

Important tables:

- `companies`
- `yc_job_postings`
- `career_page_discovery_events`: raw evidence, including YC job URL observations.
- `company_career_pages`: clean deduped external career/jobs/ATS URLs.

Useful view:

- `company_primary_career_pages`: one best external career URL per company.

Personal candidate data is ignored and should stay local:

- `data/resume/`
- `data/profile/`
- `data/runs/`
- `data/cache/`

Do not expose resume/profile contents through the API unless the user explicitly asks for that.

## Commands

```bash
uv sync --extra dev
uv run uvicorn yc_radar.main:app --reload
uv run pytest
uv run ruff check src tests scripts extract_yc_companies.py
uv run python extract_yc_companies.py
uv run python scripts/discover_career_urls.py --limit 100 --concurrency 10
uv run python scripts/generate_weekly_targets.py --no-verify-hiring --no-llm --limit 5 --candidate-pool 10
docker compose up --build
```

Use `uv`; do not add pip workflows back into the docs.

## Implementation Preferences

- Keep deterministic logic in services/scripts/playbooks.
- Keep OpenAI-dependent behavior in `agents/` and behind explicit flags like `use_llm`.
- Use SQLAlchemy for DB writes/reads; avoid ad hoc SQLite string work in app code.
- Preserve API response shapes when changing persistence.
- Keep network-heavy scripts resumable or cached where possible.
- Use table and column names that are obvious in TablePlus. Prefer names like
  `company_career_pages` over abstract names like `surfaces`.
- Tests should mock network behavior.
- Generated YC snapshots and `data/yc_radar.db` can be committed when the user asks to refresh
  or inspect data.

## Scraping And Enrichment Rules

Career URL discovery should stay cheap:

- No Firecrawl.
- No dynamic browser scraping.
- No broad domain crawls.
- Fetch homepage, `robots.txt`, capped sitemaps, and a small fixed probe list.
- Cache HTTP responses in `data/cache/career_url_discovery.json`.

Firecrawl belongs in live hiring verification, not bulk discovery. Keep it free-plan-safe:
exact pages only, at most three pages per company, low concurrency, and cached results.

## Commit Style

Use short Conventional Commit-style subjects when possible:

- `feat: add sqlite persistence and career page discovery`
- `docs: refresh project guide`
- `fix: preserve visa fields in yc job ingestion`

Before committing code changes, run:

```bash
uv run pytest
uv run ruff check src tests scripts extract_yc_companies.py
```

For docs-only changes, a pytest smoke run is usually enough, but lint/test is still preferred
when generated data or scripts changed.
