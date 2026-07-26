from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Engine

from yc_radar.adapters.greenhouse import GreenhouseAdapter
from yc_radar.services.database import fetch_company_career_page_rows, url_inventory_writer_lock
from yc_radar.services.job_repository import JobRepository


def discover_greenhouse_sources(engine: Engine) -> dict[str, Any]:
    """Register boards while cleanup cannot invalidate their provenance pages."""
    with url_inventory_writer_lock(engine):
        return _discover_greenhouse_sources_locked(engine)


def _discover_greenhouse_sources_locked(engine: Engine) -> dict[str, Any]:
    adapter = GreenhouseAdapter()
    repository = JobRepository(engine)
    now = datetime.now(UTC)
    registered = 0
    existing = 0
    skipped = 0
    conflicts: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, str]] = set()
    for page in fetch_company_career_page_rows(engine):
        company_id = page.get("company_id")
        url = str(page.get("career_page_url") or "")
        if company_id is None:
            skipped += 1
            continue
        token = adapter.extract_board_token(url)
        if token is None:
            skipped += 1
            continue
        pair = (int(company_id), token)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        source, allowed, created = repository.register_career_source(
            company_id=int(company_id),
            provider=adapter.provider,
            source_kind="ats_board",
            external_source_id=token,
            source_url=url,
            discovered_from_url=url,
            now=now,
        )
        if not allowed:
            conflicts.append(
                {
                    "board_token": token,
                    "existing_company_id": source["company_id"],
                    "requested_company_id": company_id,
                }
            )
            continue
        if created:
            registered += 1
        else:
            existing += 1
    return {
        "registered": registered,
        "existing": existing,
        "skipped": skipped,
        "conflicts": conflicts,
    }
