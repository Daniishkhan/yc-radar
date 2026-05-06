# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11 FastAPI project using a `src` package layout. Application code lives in `src/yc_radar/`: `main.py` creates the FastAPI app, `api/routes.py` defines endpoints, `domain/models.py` holds Pydantic models, `services/` loads company data, `playbooks/` builds deterministic mission output, `agents/` contains LLM-assisted refinements, and `core/config.py` handles settings. Tests live in `tests/`. Local YC exports and ranked target CSVs live in `data/`. `extract_yc_companies.py` refreshes those data files.

## Build, Test, and Development Commands

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Set up an editable install with dev tools.

```bash
uvicorn yc_radar.main:app --reload
pytest
ruff check src tests extract_yc_companies.py
python extract_yc_companies.py
docker compose up --build
```

Use `uvicorn` for local API development, `pytest` for the test suite, `ruff check` for linting, the extractor to refresh `data/`, and Docker Compose for a containerized run.

## Coding Style & Naming Conventions

Use 4-space indentation, type hints on public functions, and Python 3.11 syntax. Ruff is configured for a 100-character line length. Prefer `snake_case` for modules, functions, variables, and route handlers; use `PascalCase` for classes and Pydantic models. Keep deterministic business logic in `playbooks/` and data access in `services/`; only put OpenAI-dependent behavior in `agents/`.

## Testing Guidelines

Tests use `pytest` and should be named `tests/test_*.py` with functions named `test_*`. Use `fastapi.testclient.TestClient` for API behavior and service-level tests for repository/data loading. Keep tests deterministic and local; avoid network calls in tests. Run `pytest` before opening a PR.

## Commit & Pull Request Guidelines

There are no commits in the current repository history, so no local convention is established yet. Until one exists, use short imperative commit subjects such as `Add target filtering tests` or a Conventional Commits style like `feat: add outreach brief endpoint`.

Pull requests should include a brief summary, test commands run, linked issue or motivation, and sample API output or screenshots when endpoint behavior changes. Call out intentional changes to files under `data/`.

## Security & Configuration Tips

Copy `.env.example` to `.env` for local settings. Do not commit secrets. `OPENAI_API_KEY` is optional and should only be required for LLM-refined outreach paths.
