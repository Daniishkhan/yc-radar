from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.database import (
    career_sources_table,
    engine_from_url,
    replace_career_page_data,
)
from yc_radar.services.source_activation import activate_discovered_sources
from yc_radar.services.job_source_registry import default_job_source_providers
from yc_radar.services import source_activation


def career_page(company_id: int, slug: str, name: str, url: str) -> dict:
    return {
        "company_id": company_id,
        "company_slug": slug,
        "company_name": name,
        "career_page_url": url,
        "normalized_url": url,
        "page_type": "ats",
        "discovery_source": "test",
        "checked_at": datetime(2026, 1, 1, tzinfo=UTC),
    }


def test_checkpointed_provider_inventory_is_frozen_deduplicated_and_resumable(
    postgres_database_url: str, tmp_path
) -> None:
    engine = engine_from_url(postgres_database_url)
    companies = CompanyRegistry(engine)
    ashby_company = companies.register_company(
        name="Ashby Company", website="https://ashby-company.example"
    )
    other = companies.register_company(name="Other", website="https://other.example")
    pages = [
        career_page(
            ashby_company.company_id,
            "ashby-company",
            "Ashby Company",
            "https://jobs.ashbyhq.com/ashby-company/jobs/first",
        ),
        career_page(
            ashby_company.company_id,
            "ashby-company",
            "Ashby Company",
            "https://jobs.ashbyhq.com/ashby-company/jobs/second",
        ),
        career_page(
            other.company_id,
            "other",
            "Other",
            "https://job-boards.greenhouse.io/other/jobs/1",
        ),
    ]
    replace_career_page_data(
        engine,
        discovery_events=[],
        career_pages=pages,
        company_slugs=["ashby-company", "other"],
    )
    checkpoint = tmp_path / "ashby-registration.json"

    first = activate_discovered_sources(
        engine,
        provider="ashby",
        checkpoint_file=checkpoint,
        checkpoint_every=1,
    )
    second = activate_discovered_sources(
        engine,
        provider="ashby",
        checkpoint_file=checkpoint,
        checkpoint_every=1,
    )

    assert first == second
    assert first["selected"] == 1
    assert first["registered"] == 1
    assert first["existing"] == 0
    assert first["skipped"] == 1
    assert first["observed_rows"] == 3
    assert first["conflicts"] == []
    persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
    candidate = next(iter(persisted["candidates"].values()))
    assert candidate["state"] == "registered"
    assert candidate["attempts"] == 1
    assert candidate["observed_urls"] == sorted(
        [pages[0]["career_page_url"], pages[1]["career_page_url"]]
    )
    with engine.connect() as connection:
        source = connection.execute(select(career_sources_table)).mappings().one()
    assert source["provider"] == "ashby"
    assert source["external_source_id"] == "ashby-company"
    assert source["raw_json"]["evidence"]["candidate_key"] == candidate["candidate_key"]


def test_ambiguous_board_ownership_is_rejected_before_any_registration(
    postgres_database_url: str, tmp_path
) -> None:
    engine = engine_from_url(postgres_database_url)
    companies = CompanyRegistry(engine)
    one = companies.register_company(name="One", website="https://one.example")
    two = companies.register_company(name="Two", website="https://two.example")
    pages = [
        career_page(one.company_id, "one", "One", "https://jobs.ashbyhq.com/shared/jobs/1"),
        career_page(two.company_id, "two", "Two", "https://jobs.ashbyhq.com/shared/jobs/2"),
    ]
    replace_career_page_data(
        engine,
        discovery_events=[],
        career_pages=pages,
        company_slugs=["one", "two"],
    )

    result = activate_discovered_sources(
        engine,
        provider="ashby",
        checkpoint_file=tmp_path / "ambiguous.json",
        checkpoint_every=1,
    )

    assert result["selected"] == 2
    assert result["registered"] == 0
    assert len(result["conflicts"]) == 2
    assert all(
        conflict["error"]["class"] == "AmbiguousCompanyOwnership"
        for conflict in result["conflicts"]
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(career_sources_table)) == 0


def test_checkpoint_inventory_tampering_fails_closed(
    postgres_database_url: str, tmp_path
) -> None:
    engine = engine_from_url(postgres_database_url)
    company = CompanyRegistry(engine).register_company(
        name="One", website="https://one.example"
    )
    replace_career_page_data(
        engine,
        discovery_events=[],
        career_pages=[
            career_page(
                company.company_id,
                "one",
                "One",
                "https://jobs.ashbyhq.com/one/jobs/1",
            )
        ],
        company_slugs=["one"],
    )
    checkpoint = tmp_path / "ashby.json"
    activate_discovered_sources(
        engine,
        provider="ashby",
        checkpoint_file=checkpoint,
        checkpoint_every=1,
    )
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    next(iter(payload["candidates"].values()))["company_id"] += 100
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="inventory was modified"):
        activate_discovered_sources(
            engine,
            provider="ashby",
            checkpoint_file=checkpoint,
            checkpoint_every=1,
        )


def test_failed_registration_resumes_same_frozen_candidate(
    monkeypatch, tmp_path
) -> None:
    pages = [
        {
            "company_id": 42,
            "career_page_url": "https://jobs.ashbyhq.com/acme/jobs/first",
        }
    ]
    calls = 0

    class FakeRegistry:
        def __init__(self, engine, *, providers):
            del engine
            self.providers = providers

        def register_url(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary database interruption")
            return SimpleNamespace(
                provider="ashby",
                external_source_id="acme",
                company_id=kwargs["company_id"],
                career_source_id=7,
                created=True,
            )

    monkeypatch.setattr(source_activation, "JobSourceRegistry", FakeRegistry)
    monkeypatch.setattr(
        source_activation,
        "fetch_company_career_page_rows",
        lambda engine: pages,
    )
    checkpoint = tmp_path / "resume.json"
    providers = default_job_source_providers()

    with pytest.raises(RuntimeError, match="temporary database interruption"):
        activate_discovered_sources(
            object(),
            provider="ashby",
            checkpoint_file=checkpoint,
            checkpoint_every=1,
            providers=providers,
        )
    failed = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert next(iter(failed["candidates"].values()))["state"] == "failed"

    result = activate_discovered_sources(
        object(),
        provider="ashby",
        checkpoint_file=checkpoint,
        checkpoint_every=1,
        providers=providers,
    )

    assert result["registered"] == 1
    assert result["pending"] == 0
    persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
    candidate = next(iter(persisted["candidates"].values()))
    assert candidate["attempts"] == 2
    assert candidate["state"] == "registered"
