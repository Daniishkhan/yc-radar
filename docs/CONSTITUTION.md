# Engineering Constitution

These principles govern design decisions in YC Radar. Prefer the smallest implementation that
preserves them.

## 1. Companies Are First-Class

A company exists independently of YC, an ATS, a crawl, or a vendor. External identities attach
through `company_sources`; they do not define a second employer record.

## 2. Evidence Before Conclusions

Keep raw provenance, normalized facts, and inferred labels distinguishable. Never turn a remote
label into a visa/work-authorization claim or a weak name match into a company merge.

## 3. Identity Must Fail Closed

Use stable provider IDs, canonical URLs, verified domains, and other independent anchors. When
evidence is ambiguous, preserve a provisional identity or quarantine the record rather than risk a
false merge.

## 4. One Canonical Lifecycle

Every provider reaches jobs through `JobSyncService`. Complete snapshots may express absence;
partial scans and observations may not. Do not add side-door writes that bypass lifecycle rules.

## 5. Optimize Recall Before Ranking

Ingest the broad source inventory before role, company, or geography filters. Keep uncertain jobs
visible in a verification lane. Ranking can be strict; discovery should not silently discard
potential evidence.

## 6. Preserve Source Assertions

Two providers reporting a similar job are two assertions. Store both. Cluster at read time only
with strong anchors, and never merge distinct IDs from the same source.

## 7. Dirty Data Is Normal

Bulk inputs must be bounded, idempotent, resumable, and inspectable. One malformed or oversized row
must not abort unrelated valid work. Retain hashes, errors, and lineage sufficient to reproduce a
decision.

## 8. Work Must Be Recoverable

Use short database transactions, explicit run keys, leases, bounded retries, checkpoints, and
terminal states. A worker crash must not require manual database surgery.

## 9. Prefer Deterministic Core Logic

Identity, lifecycle, parsing, classification, and ranking defaults belong in testable services.
LLMs and paid enrichment may refine results, but remain optional and cannot be the only route to a
usable inventory.

## 10. Be a Good Public-Data Citizen

Use documented unauthenticated endpoints and bounded crawl queries. Identify the project, fetch
sequentially and politely, honor `Retry-After`, cache responses, and never submit applications or
evade access controls.

## 11. Keep the Product Script-First

Prefer small commands, Postgres tables, and local CSV/JSON artifacts. Do not introduce servers,
public endpoints, browser automation, or orchestration platforms without a concrete requirement.

## 12. Schema, Tests, and Docs Move Together

Alembic is authoritative. Schema changes require migration and regression tests; behavior changes
require deterministic tests; operator-facing changes require command examples to be updated.

Before handoff, run:

```bash
uv run pytest
uv run alembic check
uv run ruff check src tests scripts migrations
git diff --check
```

## Decision Check

Before adding a table, script, or service, ask:

1. Is this canonical state, raw evidence, staging work, or an output?
2. Can an existing company/source/job contract represent it?
3. What stable identity and provenance support the write?
4. Is the operation idempotent and recoverable after interruption?
5. Can bad input be isolated without losing good input?
6. Does the change improve source coverage or decision quality enough to justify its complexity?
