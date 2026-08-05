import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from yc_radar.adapters.lever import LeverAdapter, normalize_lever_job


def _fixture() -> list[dict[str, object]]:
    return json.loads((Path(__file__).parent / "fixtures" / "lever_jobs.json").read_text())


def test_extract_source_id_accepts_public_global_and_eu_urls() -> None:
    adapter = LeverAdapter()

    assert adapter.extract_source_id("https://jobs.lever.co/Acme") == "acme"
    assert (
        adapter.extract_source_id(
            "https://jobs.lever.co/acme/832ce96e-28f9-4f03-95e1-8a4e6ec667f0/apply"
        )
        == "acme"
    )
    assert adapter.extract_source_id("https://api.lever.co/v0/postings/acme") == "acme"
    assert adapter.extract_source_id("https://jobs.eu.lever.co/Acme-EU") == "eu:acme-eu"
    assert adapter.extract_source_id("https://api.eu.lever.co/v0/postings/acme-eu") == "eu:acme-eu"
    assert adapter.canonical_source_url("acme") == "https://jobs.lever.co/acme"
    assert adapter.canonical_source_url("EU:Acme-EU") == "https://jobs.eu.lever.co/acme-eu"


def test_extract_source_id_rejects_untrusted_or_malformed_urls() -> None:
    adapter = LeverAdapter()

    assert adapter.extract_source_id("https://example.com/acme") is None
    assert adapter.extract_source_id("https://user@jobs.lever.co/acme") is None
    assert adapter.extract_source_id("https://jobs.lever.co/%2Fetc") is None
    assert adapter.extract_source_id("https://api.lever.co/v1/postings/acme") is None
    assert adapter.extract_source_id("https://api.lever.co/v0/postings") is None


def test_normalization_preserves_complete_content_and_structured_location_evidence() -> None:
    payload = _fixture()[0]
    job = normalize_lever_job(payload)
    republished = normalize_lever_job({**payload, "createdAt": 1782993600000})

    assert job.external_job_id == "832ce96e-28f9-4f03-95e1-8a4e6ec667f0"
    assert job.location == "Remote - Americas / Toronto, Canada"
    assert job.department == "Engineering / Platform"
    assert job.employment_type == "Full-time"
    assert job.source_published_at == datetime(2026, 7, 1, 12, tzinfo=UTC)
    assert "Requirements Design reliable Python services." in (job.description_text or "")
    assert "Candidates must be based in the Americas." in (job.description_text or "")
    assert job.structured_evidence["workplace"] == {"type": "remote", "is_remote": True}
    assert job.structured_evidence["primary_location"] == {
        "label": "Remote - Americas",
        "country": "CA",
    }
    assert job.structured_evidence["secondary_locations"] == [{"label": "Toronto, Canada"}]
    assert job.structured_evidence["countries"] == ["CA"]
    assert job.structured_evidence["eligibility_signals"] == []
    # Provider freshness is lifecycle evidence, not substantive job content.
    assert republished.source_published_at == datetime(2026, 7, 2, 12, tzinfo=UTC)
    assert job.content_hash == republished.content_hash


@pytest.mark.parametrize(
    "created_at",
    [None, "1782907200000", True, 1782907200, -1, -(10**12), float("nan"), 10**30],
)
def test_normalization_ignores_invalid_or_non_millisecond_created_at(created_at: object) -> None:
    payload = {**_fixture()[0], "createdAt": created_at}

    assert normalize_lever_job(payload).source_published_at is None


def test_adapter_fetches_every_page_sequentially_from_public_get_endpoint() -> None:
    requests: list[httpx.Request] = []
    fixture = _fixture()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        skip = int(request.url.params["skip"])
        return httpx.Response(200, json=fixture[skip : skip + 1])

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await LeverAdapter(client=client, page_size=1).fetch_snapshot("acme")

    snapshot = asyncio.run(exercise())

    assert snapshot.is_complete is True
    assert snapshot.http_status == 200
    assert len(snapshot.jobs) == 2
    assert len(requests) == 3
    assert all(request.method == "GET" for request in requests)
    assert [request.url.params["skip"] for request in requests] == ["0", "1", "2"]
    assert all(request.url.params["mode"] == "json" for request in requests)
    assert all(request.url.params["limit"] == "1" for request in requests)
    assert str(requests[0].url).startswith("https://api.lever.co/v0/postings/acme?")
    assert requests[0].headers["user-agent"] == LeverAdapter.user_agent
    assert requests[0].headers["accept"] == "application/json"
    assert snapshot.request_metadata["pages_requested"] == 3


def test_adapter_uses_eu_endpoint_and_retries_rate_limits() -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    responses = [
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, json=[]),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await LeverAdapter(client=client, sleeper=sleeper).fetch_snapshot("eu:acme")

    snapshot = asyncio.run(exercise())

    assert snapshot.is_complete is True
    assert snapshot.external_source_id == "eu:acme"
    assert len(requests) == 2
    assert requests[0].url.host == "api.eu.lever.co"
    assert sleeps == [3.0]


def test_duplicate_ids_make_snapshot_incomplete() -> None:
    fixture = _fixture()
    pages = [[fixture[0]], [fixture[0]], []]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages.pop(0))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await LeverAdapter(client=client, page_size=1).fetch_snapshot("acme")

    snapshot = asyncio.run(exercise())

    assert snapshot.is_complete is False
    assert snapshot.errors == [
        {"kind": "duplicate_external_job_id", "message": "duplicate job IDs"}
    ]
