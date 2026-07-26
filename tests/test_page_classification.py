import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from yc_radar.services.database import (
    engine_from_url,
    fetch_page_classification_rows,
    fetch_source_document_rows,
)
from yc_radar.services.run_status import stage_checkpoint, stage_started, write_status

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "classify_discovered_urls.py"
SPEC = importlib.util.spec_from_file_location("classify_discovered_urls", SCRIPT_PATH)
assert SPEC and SPEC.loader
classify_discovered_urls = importlib.util.module_from_spec(SPEC)
sys.modules["classify_discovered_urls"] = classify_discovered_urls
SPEC.loader.exec_module(classify_discovered_urls)


def test_classifier_identifies_individual_job_detail_page() -> None:
    result = classify_discovered_urls.classify_page(
        url="https://example.com/careers/senior-backend-engineer",
        title="Senior Backend Engineer at Example",
        text=(
            "Senior Backend Engineer About the role Apply now Responsibilities "
            "Requirements Python Postgres distributed systems"
        ),
        http_status=200,
        url_kind="jobs_page",
    )

    assert result.page_kind == "job_detail"
    assert result.job_title == "Senior Backend Engineer"


def test_classifier_identifies_general_career_listing_page() -> None:
    result = classify_discovered_urls.classify_page(
        url="https://example.com/careers",
        title="Careers at Example",
        text=(
            "Careers Current openings Open roles Senior Backend Engineer "
            "Product Designer Forward Deployed Engineer"
        ),
        http_status=200,
        url_kind="careers_page",
    )

    assert result.page_kind == "job_listing"
    assert result.job_title is None
    assert len(result.role_titles) >= 2


def test_classifier_keeps_ats_index_separate_from_job_details() -> None:
    result = classify_discovered_urls.classify_page(
        url="https://jobs.ashbyhq.com/example",
        title="Example Jobs",
        text="Open roles Senior Software Engineer Product Manager Apply to any matching role.",
        http_status=200,
        url_kind="ats",
    )

    assert result.page_kind == "ats_listing"
    assert result.job_title is None


def test_short_role_terms_do_not_match_inside_company_slug() -> None:
    result = classify_discovered_urls.classify_page(
        url="https://jobs.ashbyhq.com/aiprise",
        title="AiPrise Jobs",
        text="AiPrise Jobs We're hiring",
        http_status=200,
        url_kind="ats",
    )

    assert result.page_kind == "ats_listing"
    assert result.job_title is None


def test_run_checkpoints_classifications_in_bounded_batches(monkeypatch, tmp_path: Path) -> None:
    rows = [{"id": row_id} for row_id in range(5)]
    persisted_batch_sizes: list[int] = []
    client_instances = []

    class FakeCachedHttpClient:
        def __init__(self, cache_path: Path, *, concurrency: int, **kwargs) -> None:
            self.cache_metrics = {"stores": 3}
            client_instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def fake_fetch_and_classify(row, http, **kwargs):
        return {"row_id": row["id"], "classification": {"evidence": {"fetch": {}}}}

    def fake_persist_results(engine, results):
        persisted_batch_sizes.append(len(results))
        return classify_discovered_urls.Counter({"career_home": len(results)}), 0

    monkeypatch.setattr(
        classify_discovered_urls,
        "engine_from_url",
        lambda: SimpleNamespace(url=SimpleNamespace(database="test")),
    )
    monkeypatch.setattr(
        classify_discovered_urls,
        "fetch_discovered_url_rows",
        lambda engine, **kwargs: rows,
    )
    monkeypatch.setattr(classify_discovered_urls, "CachedHttpClient", FakeCachedHttpClient)
    monkeypatch.setattr(classify_discovered_urls, "fetch_and_classify", fake_fetch_and_classify)
    monkeypatch.setattr(classify_discovered_urls, "persist_results", fake_persist_results)
    monkeypatch.setattr(
        classify_discovered_urls,
        "fetch_page_classification_rows",
        lambda engine, **kwargs: [],
    )
    monkeypatch.setattr(classify_discovered_urls, "write_csv", lambda *args: None)

    args = SimpleNamespace(
        limit=100,
        concurrency=2,
        batch_size=2,
        force=False,
        cache_path=tmp_path / "cache.json",
        output_csv=tmp_path / "classifications.csv",
    )
    asyncio.run(classify_discovered_urls.run(args))

    assert persisted_batch_sizes == [2, 2, 1]
    assert client_instances[0].cache_metrics == {"stores": 3}


def test_run_writes_progress_status_after_each_persisted_batch(monkeypatch, tmp_path: Path) -> None:
    rows = [{"id": row_id} for row_id in range(3)]
    statuses: list[dict] = []

    class FakeCachedHttpClient:
        def __init__(self, *args, **kwargs) -> None:
            self.cache_metrics = {"stores": 3}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def fake_fetch_and_classify(row, http, **kwargs):
        return {"row_id": row["id"], "classification": {"evidence": {"fetch": {}}}}

    monkeypatch.setattr(
        classify_discovered_urls,
        "engine_from_url",
        lambda: SimpleNamespace(url=SimpleNamespace(database="test")),
    )
    monkeypatch.setattr(classify_discovered_urls, "fetch_discovered_url_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(classify_discovered_urls, "CachedHttpClient", FakeCachedHttpClient)
    monkeypatch.setattr(classify_discovered_urls, "fetch_and_classify", fake_fetch_and_classify)
    monkeypatch.setattr(
        classify_discovered_urls,
        "persist_results",
        lambda engine, results: (classify_discovered_urls.Counter({"career_home": len(results)}), 0),
    )
    monkeypatch.setattr(classify_discovered_urls, "fetch_page_classification_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(classify_discovered_urls, "write_csv", lambda *args: None)
    monkeypatch.setattr(classify_discovered_urls, "write_status", lambda _path, payload: statuses.append(payload))

    args = SimpleNamespace(
        limit=3,
        concurrency=2,
        batch_size=2,
        force=False,
        cache_path=tmp_path / "cache.json",
        output_csv=tmp_path / "classifications.csv",
        status_file=tmp_path / "status.json",
    )
    asyncio.run(classify_discovered_urls.run(args))

    assert [status["processed"] for status in statuses if status["state"] == "running"] == [0, 0, 2, 3]


def test_main_exception_preserves_last_classification_checkpoint(
    monkeypatch, tmp_path: Path
) -> None:
    status_file = tmp_path / "status.json"
    args = SimpleNamespace(status_file=status_file)

    async def fail_after_checkpoint(_args) -> None:
        write_status(
            status_file,
            stage_checkpoint(
                stage_started("classification"),
                selected=4,
                processed=2,
                succeeded=1,
                failed=1,
            ),
        )
        raise RuntimeError("boom")

    monkeypatch.setattr(classify_discovered_urls, "parse_args", lambda: args)
    monkeypatch.setattr(classify_discovered_urls, "run", fail_after_checkpoint)

    with pytest.raises(RuntimeError, match="boom"):
        classify_discovered_urls.main()

    status = __import__("json").loads(status_file.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["processed"] == 2
    assert status["succeeded"] == 1
    assert status["error"]["message"] == "boom"


def test_persist_results_rolls_back_document_and_classification_when_job_promotion_fails(
    monkeypatch, postgres_database_url: str
) -> None:
    engine = engine_from_url(postgres_database_url)
    result = {
        "document": {
            "company_slug": "example",
            "company_name": "Example",
            "source_type": "career_url",
            "source_key": "example:job-detail",
            "url": "https://example.com/jobs/software-engineer",
            "normalized_url": "https://example.com/jobs/software-engineer",
            "title": "Software Engineer",
            "raw_text": "Software Engineer Apply now",
            "clean_text": "Software Engineer Apply now Requirements",
            "content_hash": "job-detail-content",
        },
        "classification": {
            "company_slug": "example",
            "company_name": "Example",
            "url": "https://example.com/jobs/software-engineer",
            "normalized_url": "https://example.com/jobs/software-engineer",
            "page_kind": "job_detail",
            "confidence": 0.9,
            "parser_name": "test_parser",
            "parser_version": "test",
            "http_status": 200,
            "job_title": "Software Engineer",
            "role_titles": ["Software Engineer"],
            "job_count": 1,
            "evidence": {},
        },
    }

    def fail_promotion(connection, jobs) -> None:
        del connection, jobs
        raise RuntimeError("promotion failed")

    monkeypatch.setattr(
        classify_discovered_urls, "upsert_external_job_postings_connection", fail_promotion
    )

    with pytest.raises(RuntimeError, match="promotion failed"):
        classify_discovered_urls.persist_results(engine, [result])

    assert fetch_source_document_rows(engine, source_keys=["example:job-detail"]) == []
    assert fetch_page_classification_rows(engine) == []
