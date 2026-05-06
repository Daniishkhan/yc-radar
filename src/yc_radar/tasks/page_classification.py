from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

from yc_radar.services.database import (
    engine_from_url,
    fetch_discovered_url_row,
)
from yc_radar.worker import celery_app


def _load_classifier_module() -> Any:
    module_name = "yc_radar_script_classify_discovered_urls"
    if module_name in sys.modules:
        return sys.modules[module_name]
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "classify_discovered_urls.py"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load classifier script from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


classifier_module = _load_classifier_module()
CachedHttpClient = classifier_module.CachedHttpClient
fetch_and_classify = classifier_module.fetch_and_classify
persist_classification_results = classifier_module.persist_classification_results


@celery_app.task(
    bind=True,
    name="yc_radar.classify_discovered_url",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 2},
)
def classify_discovered_url(self: Any, discovered_url_id: int) -> dict[str, Any]:
    return classify_discovered_url_once(discovered_url_id)


def classify_discovered_url_once(
    discovered_url_id: int,
    *,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    engine = engine_from_url()
    row = fetch_discovered_url_row(engine, discovered_url_id)
    if row is None:
        raise ValueError(f"Discovered URL id {discovered_url_id} was not found or is inactive.")

    result = asyncio.run(fetch_and_classify_one(row, cache_path=cache_path))
    summary = persist_classification_results(engine, [result])
    classification = result["classification"]
    return {
        "discovered_url_id": discovered_url_id,
        "company_slug": row["company_slug"],
        "url": classification["url"],
        "page_kind": classification["page_kind"],
        "http_status": classification["http_status"],
        "external_job_count": summary["external_job_count"],
    }


async def fetch_and_classify_one(
    row: dict[str, Any],
    *,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    async with CachedHttpClient(cache_path, concurrency=1) as http:
        return await fetch_and_classify(row, http)
