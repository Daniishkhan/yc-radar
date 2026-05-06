# YC Radar

YC Radar is a FastAPI workbench for turning the YC company directory into practical senior-engineer entry points: target lists, prototype missions, open-source PR ideas, founder outreach briefs, and eventually autonomous scouting agents.

The first version is intentionally grounded and boring in the right places:

- `data/yc_companies_raw.json` keeps the public YC/Algolia export.
- `data/yc_companies_prototype_targets.csv` ranks companies for prototype outreach.
- The API reads local data first, so it works before we wire in databases or queues.
- Playbooks are deterministic by default, then can be refined by an LLM when `OPENAI_API_KEY` is set.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn yc_radar.main:app --reload
```

Open:

- API: `http://localhost:8000`
- Docs: `http://localhost:8000/docs`
- Top targets: `http://localhost:8000/targets`

## Refresh YC Data

```bash
python extract_yc_companies.py
```

The extractor queries the same public Algolia index used by `ycombinator.com/companies`, splits by batch to avoid the 1,000-result cap, and writes JSON/CSV outputs into `data/`.

## One-Time Candidate Knowledge Base

Put the resume PDF at `data/resume/resume.pdf`, then run:

```bash
python scripts/ingest_resume.py
```

This writes:

- `data/profile/resume_text.txt`
- `data/profile/candidate_profile.json`

Those files are intentionally ignored by git because they contain personal information.
They are local inputs for future agent scripts and should not be exposed through the API.

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
