# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11 FastAPI project using a `src` package layout. Application code lives in `src/yc_radar/`: `main.py` creates the FastAPI app, `api/routes.py` defines endpoints, `domain/models.py` holds Pydantic models, `services/` loads company data, `playbooks/` builds deterministic mission output, `agents/` contains LLM-assisted refinements, and `core/config.py` handles settings. Tests live in `tests/`. Local YC exports and ranked target CSVs live in `data/`. `extract_yc_companies.py` refreshes those data files.

## Build, Test, and Development Commands

```bash
uv sync --extra dev
```

Set up the local project environment with dev tools.

```bash
uv run uvicorn yc_radar.main:app --reload
uv run pytest
uv run ruff check src tests scripts extract_yc_companies.py
uv run python extract_yc_companies.py
uv run python scripts/generate_weekly_targets.py --no-verify-hiring --no-llm --limit 5 --candidate-pool 10
docker compose up --build
```

Use `uv run uvicorn` for local API development, `uv run pytest` for the test suite, `uv run ruff check` for linting, the extractor to refresh `data/`, the weekly target script for local candidate-fit runs, and Docker Compose for a containerized run.

## Coding Style & Naming Conventions

Use 4-space indentation, type hints on public functions, and Python 3.11 syntax. Ruff is configured for a 100-character line length. Prefer `snake_case` for modules, functions, variables, and route handlers; use `PascalCase` for classes and Pydantic models. Keep deterministic business logic in `playbooks/` and data access in `services/`; only put OpenAI-dependent behavior in `agents/`.

## Testing Guidelines

Tests use `pytest` and should be named `tests/test_*.py` with functions named `test_*`. Use `fastapi.testclient.TestClient` for API behavior and service-level tests for repository/data loading. Keep tests deterministic and local; avoid network calls in tests. Run `uv run pytest` before opening a PR.

## Commit & Pull Request Guidelines

There are no commits in the current repository history, so no local convention is established yet. Until one exists, use short imperative commit subjects such as `Add target filtering tests` or a Conventional Commits style like `feat: add outreach brief endpoint`.

Pull requests should include a brief summary, test commands run, linked issue or motivation, and sample API output or screenshots when endpoint behavior changes. Call out intentional changes to files under `data/`.

## Security & Configuration Tips

Copy `.env.example` to `.env` for local settings. Do not commit secrets. `OPENAI_API_KEY` is optional and should only be required for LLM-refined outreach paths. `FIRECRAWL_API_KEY` is optional for live hiring checks; keep runs free-plan-safe by using exact-page scraping, at most three pages per company, and no wildcard crawls.
