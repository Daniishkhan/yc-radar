from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from yc_radar.services.application_url_validation import (
    ApplicationUrlValidator,
    normalize_public_http_url,
    parse_retry_after,
    validate_queue_rows,
)
from yc_radar.services.http_cache import DiskHttpCache


def public_addresses(_host: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


def test_validator_follows_redirects_without_fetching_bodies_and_reuses_disk_cache(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/apply":
            return httpx.Response(302, headers={"Location": "/jobs/1"}, request=request)
        return httpx.Response(200, text="body need not be consumed", request=request)

    cache = DiskHttpCache(tmp_path / "cache")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    validator = ApplicationUrlValidator(
        cache,
        client=client,
        request_delay_seconds=0,
        resolver=public_addresses,
    )

    first = validator.validate("https://careers.example/apply#form")
    second = validator.validate("https://careers.example/apply")

    assert first.outcome == "live"
    assert first.final_url == "https://careers.example/jobs/1"
    assert first.redirect_count == 1
    assert first.attempt_count == 2
    assert first.cache_source == "network"
    assert second.cache_source == "disk"
    assert len(requests) == 2


def test_retry_after_is_honored_but_bounded_before_success(tmp_path: Path) -> None:
    sleeps: list[float] = []
    responses = [
        httpx.Response(429, headers={"Retry-After": "900"}),
        httpx.Response(200),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        response.request = request
        return response

    validator = ApplicationUrlValidator(
        DiskHttpCache(tmp_path / "cache"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_delay_seconds=0,
        max_retry_delay_seconds=7,
        sleeper=sleeps.append,
        resolver=public_addresses,
    )

    result = validator.validate("https://careers.example/jobs/1")

    assert result.outcome == "live"
    assert result.attempt_count == 2
    assert sleeps == [7]


def test_private_targets_and_private_redirects_are_rejected_before_request(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": "http://169.254.169.254/latest/meta-data"},
            request=request,
        )

    validator = ApplicationUrlValidator(
        DiskHttpCache(tmp_path / "cache"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_delay_seconds=0,
        resolver=public_addresses,
    )

    direct = validator.validate("http://127.0.0.1/admin")
    redirected = validator.validate("https://careers.example/apply")

    assert direct.outcome == "invalid"
    assert direct.attempt_count == 0
    assert redirected.outcome == "invalid"
    assert redirected.error == "unsafe_redirect:non_public_network_target"
    assert len(requests) == 1


def test_queue_validation_prefers_direct_application_url_and_deduplicates_batch(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = 404 if request.url.path == "/gone" else 200
        return httpx.Response(status, request=request)

    validator = ApplicationUrlValidator(
        DiskHttpCache(tmp_path / "cache"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_delay_seconds=0,
        resolver=public_addresses,
    )
    queues = {
        "application_queue": [
            {
                "job_key": "one",
                "provider": "greenhouse",
                "application_url": "https://careers.example/live",
                "posting_url": "https://careers.example/gone",
            },
            {
                "job_key": "two",
                "provider": "greenhouse",
                "apply_url": "https://careers.example/live",
            },
            {"job_key": "three", "posting_url": "https://careers.example/gone"},
            {"job_key": "four"},
        ]
    }

    report = validate_queue_rows(queues, validator)

    assert report["summary"]["queue_row_count"] == 4
    assert report["summary"]["unique_selected_url_count"] == 2
    assert report["summary"]["batch_reuse_count"] == 1
    assert report["summary"]["outcomes"] == {"dead": 1, "invalid": 1, "live": 2}
    assert report["summary"]["dead_link_rate"] == 0.333333
    assert report["validations"][0]["url_field"] == "application_url"
    assert report["validations"][1]["cache_source"] == "batch"
    assert len(requests) == 2


def test_url_normalization_and_http_date_retry_after() -> None:
    assert normalize_public_http_url("HTTPS://Example.COM/jobs?q=1#apply") == (
        "https://example.com/jobs?q=1"
    )
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    assert parse_retry_after("Wed, 05 Aug 2026 12:00:09 GMT", now=now) == 9
    assert parse_retry_after("invalid", now=now) is None
    assert parse_retry_after("-3", now=now) == 0


def test_expired_cache_entry_is_refetched(tmp_path: Path) -> None:
    now = [datetime(2026, 8, 5, 12, tzinfo=UTC)]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    validator = ApplicationUrlValidator(
        DiskHttpCache(tmp_path / "cache"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_delay_seconds=0,
        positive_cache_ttl_seconds=60,
        clock=lambda: now[0],
        resolver=public_addresses,
    )

    validator.validate("https://careers.example/jobs/1")
    now[0] += timedelta(seconds=61)
    validator.validate("https://careers.example/jobs/1")

    assert len(requests) == 2
