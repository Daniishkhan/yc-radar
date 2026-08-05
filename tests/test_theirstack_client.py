from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import httpx
import pytest

from yc_radar.services.theirstack_client import (
    CreditBalance,
    SearchResult,
    TheirStackApiError,
    TheirStackClient,
    TheirStackRequestCache,
)


API_KEY = "test-secret-theirstack-token"
SEARCH_URL = "https://api.theirstack.com/v1/jobs/search"


def preview_body(**overrides: Any) -> dict[str, Any]:
    return {
        "blur_company_data": True,
        "limit": 25,
        "page": 0,
        "posted_at_max_age_days": 30,
        **overrides,
    }


def search_payload(job_id: int = 1) -> dict[str, Any]:
    return {
        "data": [{"id": job_id, "job_title": "Software Engineer"}],
        "metadata": {"total_results": 1},
    }


def test_request_cache_hashes_canonical_body_and_separates_requests(tmp_path: Path) -> None:
    cache = TheirStackRequestCache(tmp_path)
    first = {"limit": 25, "page": 0, "filters": {"b": 2, "a": 1}}
    reordered = {"filters": {"a": 1, "b": 2}, "page": 0, "limit": 25}
    next_page = {**first, "page": 1}

    first_hash = cache.request_hash("post", SEARCH_URL, first)

    assert first_hash == cache.request_hash("POST", SEARCH_URL, reordered)
    assert first_hash != cache.request_hash("POST", SEARCH_URL, next_page)
    assert first_hash != cache.request_hash("GET", SEARCH_URL, first)

    payload = search_payload()
    assert cache.store("POST", SEARCH_URL, first, payload) == first_hash
    assert cache.load("POST", SEARCH_URL, reordered) == payload
    assert cache.load("POST", SEARCH_URL, next_page) is None
    assert not list(tmp_path.rglob(".*.json.*"))

    with pytest.raises(ValueError, match="authorization"):
        cache.store(
            "POST",
            SEARCH_URL,
            next_page,
            {"data": [], "metadata": {"Authorization": f"Bearer {API_KEY}"}},
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("limit", 0),
        ("limit", 26),
        ("limit", True),
        ("page", -1),
        ("page", 5),
        ("page", False),
    ],
)
def test_search_rejects_out_of_bounds_pagination_without_network(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid search must not reach the network")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as http_client:
        client = TheirStackClient(
            API_KEY,
            TheirStackRequestCache(tmp_path),
            client=http_client,
        )
        with pytest.raises(ValueError, match=field):
            client.search(preview_body(**{field: value}))


def test_paid_search_requires_explicit_opt_in_without_network(tmp_path: Path) -> None:
    calls = 0

    def unexpected(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("paid guard must run before the network")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as http_client:
        client = TheirStackClient(
            API_KEY,
            TheirStackRequestCache(tmp_path),
            client=http_client,
        )
        with pytest.raises(TheirStackApiError, match="allow_paid=True"):
            client.search({"limit": 1, "page": 0, "posted_at_max_age_days": 1})
        with pytest.raises(TheirStackApiError, match="allow_paid=True"):
            client.search(
                {"limit": 1, "page": 0, "posted_at_max_age_days": 1},
                allow_paid="yes",  # type: ignore[arg-type]
            )

    assert calls == 0


def test_successful_preview_is_cached_and_replayed_without_authorization_leak(
    tmp_path: Path,
) -> None:
    calls = 0
    body = preview_body()
    payload = search_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert json.loads(request.content) == body
        return httpx.Response(200, json=payload, request=request)

    cache = TheirStackRequestCache(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        first = TheirStackClient(API_KEY, cache, client=http_client).search(body)

    def unexpected(_: httpx.Request) -> httpx.Response:
        raise AssertionError("cache hit must not reach the network")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as http_client:
        replay = TheirStackClient(API_KEY, cache, client=http_client).search(body)

    assert calls == 1
    assert first == SearchResult(
        payload=payload,
        request_hash=first.request_hash,
        cache_source="network",
    )
    assert replay.payload == payload
    assert replay.request_hash == first.request_hash
    assert replay.cache_source == "disk"
    cached_text = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json")
    )
    assert API_KEY not in cached_text
    assert "Authorization" not in cached_text


def test_preview_cache_can_expire_while_paid_cache_remains_replayable(tmp_path: Path) -> None:
    cache = TheirStackRequestCache(tmp_path)
    body = preview_body()
    payload = search_payload()
    cache.store("POST", SEARCH_URL, body, payload)
    entry = next(tmp_path.rglob("*.json"))
    os.utime(entry, (1, 1))

    assert cache.load("POST", SEARCH_URL, body) == payload
    assert cache.load("POST", SEARCH_URL, body, max_age_seconds=60) is None
    with pytest.raises(ValueError, match="non-negative"):
        cache.load("POST", SEARCH_URL, body, max_age_seconds=-1)


def test_force_refresh_bypasses_a_valid_preview_cache(tmp_path: Path) -> None:
    cache = TheirStackRequestCache(tmp_path)
    body = preview_body()
    cache.store("POST", SEARCH_URL, body, search_payload(1))
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=search_payload(2), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = TheirStackClient(API_KEY, cache, client=http_client).search(
            body,
            force_refresh=True,
        )

    assert calls == 1
    assert result.cache_source == "network"
    assert result.payload == search_payload(2)


def test_cache_body_hash_separation_causes_one_call_per_distinct_body(tmp_path: Path) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(json.loads(request.content)["page"])
        calls.append(page)
        return httpx.Response(200, json=search_payload(page + 1), request=request)

    cache = TheirStackRequestCache(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TheirStackClient(API_KEY, cache, client=http_client)
        page_zero = client.search(preview_body(page=0))
        page_one = client.search(preview_body(page=1))
        replay = client.search(preview_body(page=0))

    assert calls == [0, 1]
    assert page_zero.request_hash != page_one.request_hash
    assert replay.cache_source == "disk"


def test_429_retries_with_bounded_retry_after(tmp_path: Path) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "900"},
                json={"error": "rate limited"},
                request=request,
            )
        return httpx.Response(200, json=search_payload(), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = TheirStackClient(
            API_KEY,
            TheirStackRequestCache(tmp_path),
            client=http_client,
            sleeper=sleeps.append,
        ).search(preview_body())

    assert result.cache_source == "network"
    assert calls == 2
    assert sleeps == [300.0]


def test_paid_transport_error_is_not_retried_and_redacts_token(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(f"connection failed with Bearer {API_KEY}", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TheirStackClient(
            API_KEY,
            TheirStackRequestCache(tmp_path),
            client=http_client,
        )
        with pytest.raises(TheirStackApiError) as caught:
            client.search(
                {"limit": 1, "page": 0, "posted_at_max_age_days": 1},
                allow_paid=True,
            )

    assert calls == 1
    assert caught.value.retryable is True
    assert API_KEY not in str(caught.value)
    assert "Bearer [REDACTED]" in str(caught.value)


def test_http_error_summary_does_not_expose_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "bad_request",
                    "description": f"bad credential Bearer {API_KEY}",
                }
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TheirStackClient(
            API_KEY,
            TheirStackRequestCache(tmp_path),
            client=http_client,
        )
        with pytest.raises(TheirStackApiError) as caught:
            client.search(preview_body())

    assert caught.value.status_code == 400
    assert API_KEY not in str(caught.value)
    assert "Bearer [REDACTED]" in str(caught.value)
    assert not list(tmp_path.rglob("*.json"))


def test_success_response_containing_token_is_not_cached(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [], "metadata": {"echo": API_KEY}},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TheirStackClient(
            API_KEY,
            TheirStackRequestCache(tmp_path),
            client=http_client,
        )
        with pytest.raises(TheirStackApiError, match="credential") as caught:
            client.search(preview_body())

    assert API_KEY not in str(caught.value)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize(
    "response",
    [
        lambda request: httpx.Response(200, text="not-json", request=request),
        lambda request: httpx.Response(200, json=[], request=request),
        lambda request: httpx.Response(
            200,
            json={"data": {}, "metadata": {}},
            request=request,
        ),
        lambda request: httpx.Response(
            200,
            json={"data": [], "metadata": []},
            request=request,
        ),
    ],
)
def test_search_rejects_malformed_success_responses(
    tmp_path: Path,
    response,
) -> None:
    with httpx.Client(transport=httpx.MockTransport(response)) as http_client:
        client = TheirStackClient(
            API_KEY,
            TheirStackRequestCache(tmp_path),
            client=http_client,
        )
        with pytest.raises(TheirStackApiError):
            client.search(preview_body())

    assert not list(tmp_path.rglob("*.json"))


def test_credit_balance_parses_usage_and_exposes_remaining(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v0/billing/credit-balance"
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(
            200,
            json={"api_credits": 200, "used_api_credits": 37},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        balance = TheirStackClient(
            API_KEY,
            TheirStackRequestCache(tmp_path),
            client=http_client,
        ).credit_balance()

    assert balance == CreditBalance(api_credits=200, used_api_credits=37)
    assert balance.remaining == 163
    with pytest.raises(FrozenInstanceError):
        balance.api_credits = 0  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"api_credits": "200", "used_api_credits": 0},
        {"api_credits": 200, "used_api_credits": -1},
        {"api_credits": 200},
    ],
)
def test_credit_balance_rejects_malformed_payload(tmp_path: Path, payload: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TheirStackClient(
            API_KEY,
            TheirStackRequestCache(tmp_path),
            client=http_client,
        )
        with pytest.raises(TheirStackApiError):
            client.credit_balance()
