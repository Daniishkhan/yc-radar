import asyncio
import json
from pathlib import Path

import httpx

from yc_radar.adapters.ashby import AshbyAdapter, normalize_ashby_job


def test_extract_source_id_accepts_public_ashby_board_and_job_urls() -> None:
    adapter = AshbyAdapter()

    assert adapter.extract_source_id("https://jobs.ashbyhq.com/acme") == "acme"
    assert adapter.extract_source_id("https://jobs.ashbyhq.com/ambient.ai") == "ambient.ai"
    assert adapter.extract_source_id("https://jobs.ashbyhq.com/Hamming%20AI") == "hamming ai"
    assert (
        adapter.extract_source_id(
            "https://jobs.ashbyhq.com/acme/8e0d126f-ef56-4af0-a4f7-b008f3792e66"
        )
        == "acme"
    )
    assert (
        adapter.extract_source_id("https://api.ashbyhq.com/posting-api/job-board/acme")
        == "acme"
    )


def test_extract_source_id_rejects_untrusted_or_malformed_urls() -> None:
    adapter = AshbyAdapter()

    assert adapter.extract_source_id("https://example.com/acme") is None
    assert adapter.extract_source_id("https://user@jobs.ashbyhq.com/acme") is None
    assert adapter.extract_source_id("https://jobs.ashbyhq.com/%2Fetc") is None
    assert adapter.extract_source_id("https://api.ashbyhq.com/jobPosting.list") is None
    assert adapter.extract_source_id("https://jobs.ashbyhq.com/%20acme") is None
    assert adapter.extract_source_id("https://jobs.ashbyhq.com/Greenboard") == "greenboard"
    assert adapter.canonical_source_url("Hamming AI") == "https://jobs.ashbyhq.com/hamming%20ai"


def test_normalization_preserves_public_content_and_stable_identity() -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "ashby_jobs.json").read_text())
    job = normalize_ashby_job(fixture["jobs"][0])
    republished = normalize_ashby_job(
        {**fixture["jobs"][0], "publishedAt": "2026-07-01T12:00:00.000+00:00"}
    )

    assert job.external_job_id == "8e0d126f-ef56-4af0-a4f7-b008f3792e66"
    assert job.location == "San Francisco / Remote - US"
    assert job.department == "Engineering / Platform"
    assert job.employment_type == "FullTime"
    assert job.content_hash == republished.content_hash
    assert job.raw_payload["compensation"]["compensationTierSummary"] == "$180K - $220K"


def test_adapter_fetches_listed_jobs_from_public_get_endpoint() -> None:
    requests: list[httpx.Request] = []
    fixture = json.loads((Path(__file__).parent / "fixtures" / "ashby_jobs.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=fixture)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await AshbyAdapter(client=client).fetch_snapshot("acme")

    snapshot = asyncio.run(exercise())

    assert snapshot.is_complete is True
    assert len(snapshot.jobs) == 1
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == (
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"
    )
    assert requests[0].headers["user-agent"] == AshbyAdapter.user_agent
    assert requests[0].headers["accept"] == "application/json"
