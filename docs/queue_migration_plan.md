# Queue Migration Plan

This repo should move from long synchronous scripts to a queue-backed local pipeline before the
full YC run and before heavy LLM enrichment. The goal is better observability, failure isolation,
retry control, and clean separation between orchestration state and durable intelligence data.

## Direction

Use Redis, Celery, and Flower for orchestration and live visibility.

- Redis stores active queue state and Celery result metadata.
- Celery workers execute bounded, idempotent tasks.
- Flower gives a local dashboard for running, failed, retried, and completed tasks.
- Postgres remains the durable source of truth for companies, discovered URLs, source documents,
  classifications, jobs, extraction runs, chunks, embeddings, and LLM outputs.

Redis should answer operational questions: what is running, what failed, what is retrying, and what
is blocked. Postgres should answer durable product questions: what did we discover, fetch, classify,
extract, rank, and write out.

## Queue Boundaries

Start with these Celery queues:

- `discovery`: one task per company slug.
- `fetch`: one task per discovered URL id.
- `classification`: one task per source document id or discovered URL id.
- `job_extraction`: one task per job detail source document.
- `llm_enrichment`: one task per company, job, document chunk, or shortlist item.
- `embeddings`: one task per source document or document chunk batch.

Each task must be idempotent. Re-running a task should upsert the same durable rows instead of
duplicating data or corrupting state.

## Proposed Tasks

### 1. Add Local Queue Infrastructure

- Add `redis` and `flower` services to `docker-compose.yml`.
- Add Celery dependencies to `pyproject.toml`.
- Add environment settings for broker URL, result backend, task result expiry, and worker
  concurrency.
- Document local commands in `README.md`.

Acceptance:

- `docker compose up -d postgres redis flower` starts the local stack.
- Flower is reachable locally and shows an empty Celery app.

### 2. Add Celery App Skeleton

- Add `src/yc_radar/worker.py` with the Celery app.
- Add task modules under `src/yc_radar/tasks/`.
- Configure named queues and task routing.
- Keep task payloads small: ids and slugs, not large HTML/text payloads.

Acceptance:

- `uv run celery -A yc_radar.worker inspect ping` works when a worker is running.
- A no-op smoke task can be enqueued and observed in Flower.

### 3. Convert URL Classification First

Classification is the first queue candidate because a single bad URL can currently poison the
whole batch. Convert it before discovery.

- Add `classify_discovered_url(discovered_url_id)` task.
- Fetch one URL, sanitize text, store one source document, classify it, and upsert any external job.
- Add retry policy for transient network errors.
- Record permanent parser/storage failures in task metadata and Postgres audit rows.
- Keep the existing script as an enqueuer: select unclassified URLs, enqueue tasks, optionally wait.

Acceptance:

- A 200-URL run continues past individual URL failures.
- Failed URLs are visible in Flower with exception details.
- Successful URLs have `source_documents` and `page_classifications` rows.

### 4. Add Durable Pipeline Audit Tables

Do not add workflow fields to every domain table. Add generic audit tables instead.

Suggested tables:

- `pipeline_runs`: run id, stage, status, input count, success count, failure count, started/completed.
- `pipeline_task_attempts`: run id, task name, entity type, entity id, status, attempt number, error,
  started/completed, celery task id.

Acceptance:

- Redis can expire old task state without losing historical run summaries.
- TablePlus can answer which companies/URLs failed in a specific run.

### 5. Convert Career Discovery

- Add `discover_company_career_urls(company_slug)` task.
- Preserve the existing cheap discovery rules: homepage, robots, capped sitemaps, fixed probes.
- Store discovery events and career pages per company.
- Keep current cache behavior or move HTTP cache behind a small service helper.

Acceptance:

- A failed company does not block a batch of other companies.
- Per-company failures are visible in Flower and audit rows.

### 6. Add LLM-Specific Queues

LLM work should not share workers with cheap HTTP/classification tasks.

- Add `llm_enrichment` queue with low concurrency.
- Add explicit retry/backoff for rate limits.
- Add token/cost/latency metadata to durable audit rows.
- Keep OpenAI-dependent code behind explicit flags or queue names.

Acceptance:

- LLM jobs can be paused independently.
- Token usage and failed prompts can be inspected after a run.

## 200-URL Baseline Test

Before converting classification to Celery, run the current classifier on at least 200 discovered
URLs after sanitizing Postgres text fields. Capture:

- Discovery time needed to prepare enough URLs.
- Classification wall-clock time.
- Page kind counts.
- Failed/fetch-error pages.
- Any storage or parser exceptions.

This baseline is the comparison point for the queued implementation.

## First Implementation Slice

The first queue slice converts URL classification to one Celery task per discovered URL.

Implemented pieces:

- Redis and Flower services in Docker Compose.
- Celery app at `yc_radar.worker`.
- Classification task: `yc_radar.classify_discovered_url`.
- Enqueuer script: `scripts/enqueue_classification_tasks.py`.
- Shared classification persistence helper used by both the old script and the queued task.
- NUL-byte sanitization before writing fetched text into Postgres.

Run it locally:

```bash
docker compose up -d redis flower
uv run celery -A yc_radar.worker worker -Q classification --concurrency 4 --loglevel INFO
uv run python scripts/enqueue_classification_tasks.py --limit 50 --wait
```

