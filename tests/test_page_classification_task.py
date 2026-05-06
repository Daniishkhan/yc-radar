from __future__ import annotations

from typing import Any

import pytest

from yc_radar.tasks import page_classification


def test_classify_discovered_url_once_persists_one_result(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = object()
    row = {
        "id": 123,
        "company_slug": "example",
        "normalized_url": "https://example.com/careers",
    }
    result = {
        "document": {"source_key": "example:https://example.com/careers"},
        "classification": {
            "url": "https://example.com/careers",
            "page_kind": "job_listing",
            "http_status": 200,
        },
    }
    persisted: dict[str, Any] = {}

    async def fake_fetch_and_classify_one(
        fetched_row: dict[str, Any],
        *,
        cache_path: Any = None,
    ) -> dict[str, Any]:
        assert fetched_row == row
        assert cache_path is None
        return result

    def fake_persist_classification_results(
        fetched_engine: object,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        persisted["engine"] = fetched_engine
        persisted["results"] = results
        return {"external_job_count": 0}

    monkeypatch.setattr(page_classification, "engine_from_url", lambda: engine)
    monkeypatch.setattr(
        page_classification,
        "fetch_discovered_url_row",
        lambda fetched_engine, discovered_url_id: row,
    )
    monkeypatch.setattr(
        page_classification,
        "fetch_and_classify_one",
        fake_fetch_and_classify_one,
    )
    monkeypatch.setattr(
        page_classification,
        "persist_classification_results",
        fake_persist_classification_results,
    )

    summary = page_classification.classify_discovered_url_once(123)

    assert persisted == {"engine": engine, "results": [result]}
    assert summary == {
        "discovered_url_id": 123,
        "company_slug": "example",
        "url": "https://example.com/careers",
        "page_kind": "job_listing",
        "http_status": 200,
        "external_job_count": 0,
    }


def test_classify_discovered_url_once_rejects_missing_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(page_classification, "engine_from_url", lambda: object())
    monkeypatch.setattr(
        page_classification,
        "fetch_discovered_url_row",
        lambda fetched_engine, discovered_url_id: None,
    )

    with pytest.raises(ValueError, match="123"):
        page_classification.classify_discovered_url_once(123)


def test_worker_routes_classification_to_named_queue() -> None:
    route = page_classification.celery_app.conf.task_routes["yc_radar.classify_discovered_url"]

    assert route["queue"] == "classification"

