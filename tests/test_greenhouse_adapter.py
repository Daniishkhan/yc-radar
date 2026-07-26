import asyncio
import json
from pathlib import Path

import httpx

from yc_radar.adapters.greenhouse import GreenhouseAdapter, normalize_greenhouse_job


def test_extract_board_token_accepts_common_public_greenhouse_urls() -> None:
    adapter = GreenhouseAdapter()

    assert adapter.extract_board_token("https://boards.greenhouse.io/stripe") == "stripe"
    assert (
        adapter.extract_board_token("https://job-boards.greenhouse.io/stripe/jobs/4012345")
        == "stripe"
    )
    assert (
        adapter.extract_board_token("https://boards.greenhouse.io/embed/job_board?for=stripe")
        == "stripe"
    )
    assert (
        adapter.extract_board_token("https://boards.greenhouse.io/embed/job_board/js?for=stripe")
        == "stripe"
    )
    assert (
        adapter.extract_board_token(
            "https://boards.greenhouse.io/embed/job_app?for=stripe&token=4012345"
        )
        == "stripe"
    )
    assert (
        adapter.extract_board_token("https://boards-api.greenhouse.io/v1/boards/stripe/jobs")
        == "stripe"
    )


def test_extract_board_token_rejects_untrusted_or_malformed_urls() -> None:
    adapter = GreenhouseAdapter()

    assert adapter.extract_board_token("https://example.com/stripe") is None
    assert adapter.extract_board_token("https://boards.greenhouse.io/embed/job_board?for=") is None
    assert adapter.extract_board_token("https://user@boards.greenhouse.io/stripe") is None
    assert adapter.extract_board_token("https://boards.greenhouse.io/%2Fetc") is None
    assert adapter.extract_board_token("https://boards.greenhouse.io/embed") is None
    assert adapter.extract_board_token("https://boards.greenhouse.io/embed/job_app") is None
    assert adapter.extract_board_token("https://boards.greenhouse.io/embed/unknown?for=stripe") is None
    assert (
        adapter.extract_board_token("https://boards.greenhouse.io/embed/job_board?for=one&for=two")
        is None
    )


def test_normalization_is_deterministic_and_excludes_wrapper_timestamps() -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "greenhouse_jobs.json").read_text())
    job = normalize_greenhouse_job(fixture["jobs"][0])
    same_job = normalize_greenhouse_job({**fixture["jobs"][0], "updated_at": "2026-06-01T00:00:00Z"})

    assert job.external_job_id == "4012345"
    assert job.description_text == "Build distributed API systems."
    assert job.department == "Engineering / Platform"
    assert job.content_hash == same_job.content_hash
    assert job.raw_payload["location"]["name"] == "San Francisco / Remote"


def test_adapter_uses_read_only_public_get_and_retries_rate_limits() -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    fixture = json.loads((Path(__file__).parent / "fixtures" / "greenhouse_jobs.json").read_text())
    responses = [httpx.Response(429), httpx.Response(200, json=fixture)]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = GreenhouseAdapter(client=client, sleeper=sleeper)
            return await adapter.fetch_snapshot("acme")

    snapshot = asyncio.run(exercise())

    assert snapshot.is_complete is True
    assert snapshot.http_status == 200
    assert len(snapshot.jobs) == 1
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)
    assert str(requests[0].url) == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    assert requests[0].headers["user-agent"] == GreenhouseAdapter.user_agent
    assert requests[0].headers["accept"] == "application/json"
    assert requests[0].headers["accept-language"] == "en-US,en;q=0.8"
    assert sleeps == [1.0]


def test_adapter_retries_transient_transport_errors() -> None:
    calls = 0
    sleeps: list[float] = []
    fixture = json.loads((Path(__file__).parent / "fixtures" / "greenhouse_jobs.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary connection failure", request=request)
        return httpx.Response(200, json=fixture)

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await GreenhouseAdapter(client=client, sleeper=sleeper).fetch_snapshot("acme")

    snapshot = asyncio.run(exercise())

    assert snapshot.is_complete is True
    assert calls == 2
    assert sleeps == [1.0]


def test_adapter_bounds_5xx_retries_without_applying_partial_results() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await GreenhouseAdapter(client=client, sleeper=sleeper).fetch_snapshot("acme")

    snapshot = asyncio.run(exercise())

    assert snapshot.is_complete is False
    assert snapshot.http_status == 503
    assert calls == 4
    assert sleeps == [1.0, 2.0, 4.0]
