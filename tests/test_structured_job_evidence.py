from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from yc_radar.adapters.ashby import normalize_ashby_job
from yc_radar.adapters.greenhouse import normalize_greenhouse_job
from yc_radar.domain.job_sources import SourceSnapshot
from yc_radar.services.company_registry import CompanyRegistry
from yc_radar.services.database import (
    engine_from_url,
    job_posting_versions_table,
    job_postings_table,
)
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_source_registry import JobSourceRegistry
from yc_radar.services.job_sync_service import JobSyncService


def _greenhouse_payload() -> dict[str, object]:
    return {
        "id": 4012345,
        "title": "Senior Backend Engineer",
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4012345",
        "content": "<p>Build distributed API systems.</p>",
        "location": {"name": "Remote"},
        "departments": [{"id": 1, "name": "Engineering"}],
        "offices": [
            {"id": 22, "name": "Asia", "location": "Singapore"},
            {"id": 11, "name": "Distributed", "location": "Worldwide"},
        ],
        "metadata": [
            {
                "id": 900,
                "name": "Eligible countries",
                "value_type": "multi_select",
                "value": ["Pakistan", "India"],
            },
            {"id": 901, "name": "Cost center", "value_type": "text", "value": "R&D"},
        ],
        "language": "en",
        "application_deadline": "2026-09-30T00:00:00Z",
    }


def _ashby_payload() -> dict[str, object]:
    return {
        "id": "8e0d126f-ef56-4af0-a4f7-b008f3792e66",
        "title": "Senior Backend Engineer",
        "location": "Karachi",
        "address": {
            "postalAddress": {
                "addressLocality": "Karachi",
                "addressRegion": "Sindh",
                "addressCountry": "Pakistan",
            }
        },
        "secondaryLocations": [
            {
                "location": "Singapore",
                "address": {
                    "addressLocality": "Singapore",
                    "addressCountry": "Singapore",
                },
            }
        ],
        "department": "Engineering",
        "isListed": True,
        "isRemote": True,
        "workplaceType": "Remote",
        "descriptionPlain": "Build distributed API systems.",
        "employmentType": "FullTime",
        "jobUrl": "https://jobs.ashbyhq.com/acme/8e0d126f",
        "applyUrl": "https://jobs.ashbyhq.com/acme/8e0d126f/application",
    }


def test_greenhouse_structured_evidence_is_semantic_ordered_and_content_hashed() -> None:
    payload = _greenhouse_payload()
    job = normalize_greenhouse_job(payload)
    reordered = normalize_greenhouse_job(
        {
            **payload,
            "offices": [
                {**item, "id": int(item["id"]) + 1000}
                for item in reversed(payload["offices"])  # type: ignore[arg-type]
            ],
            "metadata": [
                {**item, "id": int(item["id"]) + 1000}
                for item in reversed(payload["metadata"])  # type: ignore[arg-type]
            ],
        }
    )

    assert job.structured_evidence == reordered.structured_evidence
    assert job.content_hash == reordered.content_hash
    assert job.structured_evidence["countries"] == ["India", "Pakistan"]
    assert {
        (office.get("name"), office.get("location"))
        for office in job.structured_evidence["offices"]
    } == {
        ("Asia", "Singapore"),
        ("Distributed", "Worldwide"),
    }
    assert job.structured_evidence["eligibility_signals"] == [
        {
            "kind": "provider_metadata",
            "name": "Eligible countries",
            "value": ["Pakistan", "India"],
            "value_type": "multi_select",
        }
    ]

    changed_office = normalize_greenhouse_job(
        {
            **payload,
            "offices": [{"name": "Asia", "location": "Pakistan"}],
        }
    )
    changed_metadata = normalize_greenhouse_job(
        {
            **payload,
            "metadata": [
                {
                    "name": "Eligible countries",
                    "value_type": "multi_select",
                    "value": ["India"],
                }
            ],
        }
    )
    assert changed_office.content_hash != job.content_hash
    assert changed_metadata.content_hash != job.content_hash


def test_ashby_remote_workplace_and_address_evidence_is_content_hashed() -> None:
    payload = _ashby_payload()
    job = normalize_ashby_job(payload)

    assert job.structured_evidence["workplace"] == {"type": "remote", "is_remote": True}
    assert job.structured_evidence["primary_location"] == {
        "label": "Karachi",
        "locality": "Karachi",
        "region": "Sindh",
        "country": "Pakistan",
    }
    assert job.structured_evidence["secondary_locations"] == [
        {"label": "Singapore", "locality": "Singapore", "country": "Singapore"}
    ]
    assert job.structured_evidence["countries"] == ["Pakistan", "Singapore"]
    assert job.structured_evidence["application"]["is_listed"] is True

    onsite = normalize_ashby_job({**payload, "isRemote": False, "workplaceType": "OnSite"})
    different_country = normalize_ashby_job(
        {
            **payload,
            "address": {
                "postalAddress": {
                    "addressLocality": "Dubai",
                    "addressCountry": "United Arab Emirates",
                }
            },
        }
    )
    assert onsite.content_hash != job.content_hash
    assert different_country.content_hash != job.content_hash


def test_sync_persists_evidence_in_current_version_and_active_reads(
    postgres_database_url: str,
) -> None:
    engine = engine_from_url(postgres_database_url)
    company = CompanyRegistry(engine).register_company(
        name="Evidence Example",
        website="https://evidence.example",
    )
    source = JobSourceRegistry(engine).register_url(
        company_id=company.company_id,
        source_url="https://job-boards.greenhouse.io/evidence-example",
    )
    job = normalize_greenhouse_job(_greenhouse_payload())
    snapshot = SourceSnapshot(
        provider="greenhouse",
        external_source_id="evidence-example",
        adapter_version="3",
        is_complete=True,
        http_status=200,
        jobs=[job],
    )

    result = JobSyncService(engine, clock=lambda: datetime(2026, 8, 1, tzinfo=UTC)).sync_snapshot(
        career_source_id=source.career_source_id,
        run_key="evidence-v1",
        snapshot=snapshot,
    )
    assert result.status == "completed"

    with engine.connect() as connection:
        current = connection.execute(select(job_postings_table)).mappings().one()
        version = connection.execute(select(job_posting_versions_table)).mappings().one()
    active = JobRepository(engine).active_job_rows()

    assert current["structured_evidence"] == job.structured_evidence
    assert version["structured_evidence"] == job.structured_evidence
    assert active[0]["structured_evidence"] == job.structured_evidence
    assert version["raw_payload"] == _greenhouse_payload()
