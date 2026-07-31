from __future__ import annotations

from pathlib import Path

import httpx

from yc_radar.services.google_domain_resolver import (
    GoogleDomainResolver,
    acceptable_company_domain,
    parse_grounded_response,
)


def raw_response(
    text: str,
    *,
    queries: list[str] | None = None,
    chunks: list[dict] | None = None,
) -> dict:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "groundingMetadata": {
                    "webSearchQueries": queries or [],
                    "groundingChunks": chunks or [],
                    "groundingSupports": [{"groundingChunkIndices": [0]}],
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 4,
            "totalTokenCount": 18,
            "thoughtsTokenCount": 2,
            "cachedContentTokenCount": 1,
        },
    }


class FakeModels:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class FakeClient:
    def __init__(self, response) -> None:
        self.models = FakeModels(response)


def test_text_candidate_requires_cross_page_brand_and_exact_reciprocal_proof(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text="<title>Acme — Home</title>")
        if request.url.path == "/careers":
            return httpx.Response(
                200,
                text=(
                    '<h1>Careers</h1><a href="https://job-boards.greenhouse.io/acme">'
                    "Open roles</a>"
                ),
            )
        return httpx.Response(404)

    model = FakeClient(raw_response("https://acme.test", queries=["Acme official careers"]))
    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=model,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
    )

    result = resolver.resolve(company_name="Acme", board_token="acme")

    assert result.status == "accepted"
    assert result.accepted_domain == "acme.test"
    assert result.passing_domain_count == 1
    assert result.candidate_evidence[0].brand_valid is True
    assert result.candidate_evidence[0].reciprocal_link_valid is True
    assert len(model.models.calls) == 1


def test_missing_web_domain_and_grounding_redirect_are_supported_and_cached(
    tmp_path: Path,
) -> None:
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == redirect:
            return httpx.Response(302, headers={"Location": "https://acme.test/careers"})
        if request.url.host == "acme.test":
            return httpx.Response(
                200,
                text=(
                    "<title>Acme Careers</title>"
                    '<a href="https://boards.greenhouse.io/acme/jobs/1">Apply</a>'
                ),
            )
        return httpx.Response(404)

    response = raw_response(
        "The official domain is acme.test.",
        queries=["Acme official website", "Acme Greenhouse careers"],
        chunks=[{"web": {"uri": redirect, "title": "acme.test"}}],
    )
    model = FakeClient(response)
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=model,
        http_client=http_client,
        sleeper=lambda _: None,
        delay_seconds=0,
    )

    first = resolver.resolve(company_name="Acme", board_token="acme")
    second = resolver.resolve(company_name="Acme", board_token="acme")

    assert first.status == "accepted"
    assert first.citations[0].declared_domain == ""
    assert first.search_query_count == 2
    assert first.cache_source == "network"
    assert second.cache_source == "disk"
    assert len(model.models.calls) == 1
    assert (tmp_path / "responses.json").read_text(encoding="utf-8").find(
        "grounding-api-redirect"
    ) != -1


def test_brand_valid_without_exact_board_link_is_manual_review(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "<title>Acme Careers</title>"
                '<a href="https://job-boards.greenhouse.io/different">Jobs</a>'
            ),
        )

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(raw_response("acme.test", queries=["Acme official"])),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
    )

    result = resolver.resolve(company_name="Acme", board_token="acme")

    assert result.status == "manual_review"
    assert result.accepted_domain is None
    assert result.candidate_evidence[0].brand_valid is True
    assert result.candidate_evidence[0].reciprocal_link_valid is False


def test_transient_page_exhaustion_marks_manual_review_retryable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text="<title>Acme Home</title>")
        return httpx.Response(503)

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(raw_response("acme.test", queries=["Acme official"])),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
        max_attempts=1,
    )

    result = resolver.resolve(company_name="Acme", board_token="acme")

    assert result.status == "manual_review"
    assert result.retryable is True
    assert result.error == "retryable_page_fetch"
    assert result.candidate_evidence[0].retryable is True


def test_multiple_passing_domains_are_ambiguous(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "<title>Acme Careers</title>"
                '<a href="https://job-boards.greenhouse.io/acme">Jobs</a>'
            ),
        )

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(
            raw_response("acme.test and acme.example", queries=["Acme official domain"])
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
    )

    result = resolver.resolve(company_name="Acme", board_token="acme")

    assert result.status == "ambiguous"
    assert result.passing_domain_count == 2
    assert result.accepted_domain is None


def test_quota_errors_retry_boundedly_and_are_not_cached(tmp_path: Path) -> None:
    class QuotaError(Exception):
        code = 429

    client = FakeClient(QuotaError("RESOURCE_EXHAUSTED"))
    sleeps: list[float] = []
    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=client,
        sleeper=sleeps.append,
        delay_seconds=0,
        retry_delay_seconds=0.5,
        max_attempts=2,
    )

    result = resolver.resolve(company_name="Acme", board_token="acme")

    assert result.status == "quota_exhausted"
    assert result.quota_exhausted is True
    assert result.retryable is True
    assert result.request_attempt_count == 2
    assert sleeps == [0.5]
    assert not (tmp_path / "responses.json").exists()


def test_private_and_third_party_domains_are_not_company_candidates() -> None:
    assert acceptable_company_domain("jobs.greenhouse.io") is None
    assert acceptable_company_domain("company.local") is None
    assert acceptable_company_domain("service.internal") is None
    assert acceptable_company_domain("127.0.0.1") is None
    assert acceptable_company_domain("acme.wikipedia.org") is None
    assert acceptable_company_domain("careers.acme.test") == "acme.test"


def test_page_fetch_rejects_nonstandard_or_invalid_ports_before_network(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="<title>Acme</title>")

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(raw_response("acme.test", queries=["Acme"])),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        delay_seconds=0,
    )

    nonstandard = resolver._fetch_page("https://acme.test:8443/careers")
    invalid = resolver._fetch_page("https://acme.test:not-a-port/careers")

    assert nonstandard[4] == "invalid_url"
    assert invalid[4] == "invalid_port"
    assert requests == []


def test_parser_retains_full_grounding_metadata_and_usage() -> None:
    parsed = parse_grounded_response(
        raw_response(
            "acme.test",
            queries=["one", "two"],
            chunks=[
                {
                    "web": {
                        "uri": "https://vertexaisearch.cloud.google.com/redirect",
                        "title": "acme.test",
                    }
                }
            ],
        )
    )

    assert parsed.search_queries == ("one", "two")
    assert parsed.citations[0].declared_domain == ""
    assert parsed.grounding_metadata["groundingSupports"] == [
        {"groundingChunkIndices": [0]}
    ]
    assert parsed.prompt_token_count == 12
    assert parsed.candidates_token_count == 4
    assert parsed.total_token_count == 18
    assert parsed.thoughts_token_count == 2
    assert parsed.cached_content_token_count == 1
