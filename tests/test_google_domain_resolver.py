from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from yc_radar.services.google_domain_resolver import (
    EVIDENCE_VERSION,
    MAX_PAGE_BYTES,
    PROMPT_VERSION,
    GoogleDomainResolver,
    acceptable_company_domain,
    find_company_domain_matches,
    normalize_brand_text,
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


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/private",
        "http://service.internal/private",
    ],
)
def test_redirects_to_non_public_hosts_are_rejected_before_fetch(
    tmp_path: Path, target: str
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": target})

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(raw_response("acme.test", queries=["Acme"])),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        delay_seconds=0,
    )

    fetched = resolver._fetch_page("https://acme.test")

    assert fetched[0] is None
    assert fetched[4] == "unsafe_redirect:non_public_host"
    assert requests == ["https://acme.test"]


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


def test_prompt_requires_company_owned_non_ats_url_with_unknown_fallback(
    tmp_path: Path,
) -> None:
    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(raw_response("UNKNOWN")),
        delay_seconds=0,
    )

    request = resolver._request_identity(company_name="Acme", board_token="acme")

    assert PROMPT_VERSION == 2
    assert EVIDENCE_VERSION == 3
    assert request["prompt_version"] == PROMPT_VERSION
    assert "company-owned" in request["prompt"]
    assert "never return a greenhouse.io URL" in request["prompt"]
    assert "return UNKNOWN" in request["prompt"]


def test_search_query_domains_are_verified_but_ats_only_output_is_ignored(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
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
            raw_response(
                "https://job-boards.greenhouse.io/acme",
                queries=["Acme official careers acme.test"],
            )
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
    )

    result = resolver.resolve(company_name="Acme", board_token="acme")

    assert result.status == "accepted"
    assert result.accepted_domain == "acme.test"
    assert result.candidate_evidence[0].candidate_sources == ("search_query",)
    assert all(request.url.host != "job-boards.greenhouse.io" for request in requests)


@pytest.mark.parametrize(
    ("company_name", "board_token", "domain", "visible_brand"),
    [
        ("Ōura", "oura", "ouraring.com", "Oura"),
        ("LayerZero Labs", "layerzerolabs", "layerzero.network", "LayerZero"),
        (
            "Genius Sports Statistician Network",
            "geniussportssn",
            "geniussports.com",
            "Genius Sports",
        ),
    ],
)
def test_unicode_and_conservative_domain_aliases_recover_known_official_domains(
    tmp_path: Path,
    company_name: str,
    board_token: str,
    domain: str,
    visible_brand: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                f"<title>{visible_brand} Careers</title>"
                f'<a href="https://job-boards.greenhouse.io/{board_token}">Jobs</a>'
            ),
        )

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(raw_response(f"https://{domain}", queries=["official website"])),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
    )

    result = resolver.resolve(company_name=company_name, board_token=board_token)

    assert result.status == "accepted"
    assert result.accepted_domain == domain
    assert result.candidate_evidence[0].company_domain_compatible is True
    assert result.candidate_evidence[0].company_domain_matches


def test_nfkd_brand_normalization_preserves_ascii_base_letters() -> None:
    assert normalize_brand_text("ŌURA") == "oura"


@pytest.mark.parametrize(
    ("script", "expected_url"),
    [
        (
            '<script>window.jobs="https://job-boards.greenhouse.io/acme/jobs/1";</script>',
            "https://job-boards.greenhouse.io/acme/jobs/1",
        ),
        (
            r'<script type="application/json">{"url":"https:\/\/job-boards.greenhouse.io\/acme\/jobs\/2"}</script>',
            "https://job-boards.greenhouse.io/acme/jobs/2",
        ),
        (
            r'<script type="application/json">{"url":"https:\u002F\u002Fjob-boards.greenhouse.io\u002Facme\u002Fjobs\u002F3"}</script>',
            "https://job-boards.greenhouse.io/acme/jobs/3",
        ),
    ],
)
def test_active_inline_scripts_preserve_literal_and_json_escaped_greenhouse_proof(
    tmp_path: Path,
    script: str,
    expected_url: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"<title>Acme</title>{script}")

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(raw_response("acme.test", queries=["Acme official"])),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
    )

    result = resolver.resolve(company_name="Acme", board_token="acme")

    assert result.status == "accepted"
    assert expected_url in result.candidate_evidence[0].pages[0].greenhouse_links


@pytest.mark.parametrize("expression", ["template", "concatenation"])
def test_same_variable_script_urls_are_realized_as_auditable_proof(
    tmp_path: Path,
    expression: str,
) -> None:
    url_expression = (
        "`https://boards-api.greenhouse.io/v1/boards/${BOARD_TOKEN}/jobs`"
        if expression == "template"
        else '"https://boards-api.greenhouse.io/v1/boards/" + BOARD_TOKEN + "/jobs"'
    )
    script = f"<script>const BOARD_TOKEN='iconcareers'; const jobs={url_expression};</script>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"<title>ICON Careers</title>{script}")

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(raw_response("iconbuild.test", queries=["ICON official"])),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
    )

    result = resolver.resolve(company_name="ICON", board_token="iconcareers")

    assert result.status == "accepted"
    assert result.candidate_evidence[0].pages[0].greenhouse_links == (
        "https://boards-api.greenhouse.io/v1/boards/iconcareers/jobs",
    )


def test_html_comments_and_mismatched_script_variables_are_not_proof(tmp_path: Path) -> None:
    page = (
        "<title>Acme Careers</title>"
        '<!-- <script>"https://job-boards.greenhouse.io/acme"</script> -->'
        "<script>const TOKEN='different';"
        "const jobs=`https://boards-api.greenhouse.io/v1/boards/${TOKEN}/jobs`;</script>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

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
    assert result.candidate_evidence[0].reciprocal_link_valid is False


def test_brand_and_reciprocal_link_cannot_override_company_domain_mismatch(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "<title>Engineering at Attentive · Morning Stack</title>"
                '<a href="https://job-boards.greenhouse.io/attentive/jobs/1">Apply</a>'
            ),
        )

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(
            raw_response("companies.morningstack.test", queries=["Attentive careers"])
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
    )

    result = resolver.resolve(company_name="Attentive", board_token="attentive")
    evidence = result.candidate_evidence[0]

    assert result.status == "unresolved"
    assert evidence.brand_valid is True
    assert evidence.reciprocal_link_valid is True
    assert evidence.company_domain_compatible is False
    assert evidence.passed is False


def test_unrelated_retryable_candidate_does_not_suppress_proven_compatible_domain(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "acme.test":
            return httpx.Response(
                200,
                text=(
                    "<title>Acme Careers</title>"
                    '<a href="https://job-boards.greenhouse.io/acme">Jobs</a>'
                ),
            )
        return httpx.Response(503)

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(
            raw_response("acme.test unrelated.test", queries=["Acme official"])
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
        max_attempts=1,
    )

    result = resolver.resolve(company_name="Acme", board_token="acme")

    assert result.status == "accepted"
    assert result.accepted_domain == "acme.test"
    assert result.retryable is False


def test_known_job_aggregators_are_not_company_candidates() -> None:
    for domain in (
        "morningstack.app",
        "companies.morningstack.app",
        "employbl.com",
        "uplers.com",
        "mccoy.io",
        "substack.com",
        "workable.com",
    ):
        assert acceptable_company_domain(domain) is None


@pytest.mark.parametrize(
    ("company_name", "domain"),
    [
        ("Veterinary Practice Partners", "vetpracticepartners.com"),
        ("ALTEN Technology USA", "altenusa.com"),
        ("Highwire", "teamhighwire.com"),
        ("OKX", "okx.com"),
        ("IMC", "imc.com"),
        ("DRW", "drw.com"),
        ("ID.me", "id.me"),
        ("Via", "ridewithvia.com"),
        ("Private Equity Insights", "pe-insights.com"),
        ("Diana Health", "heydianahealth.com"),
    ],
)
def test_domain_gate_retains_conservative_known_brand_forms(
    company_name: str, domain: str
) -> None:
    assert find_company_domain_matches(domain, company_name)


def test_final_url_dedupe_and_discovered_career_link_respect_page_budget(
    tmp_path: Path,
) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(302, headers={"Location": "https://www.acme.test/landing/"})
        if request.url.path == "/landing/":
            return httpx.Response(
                200,
                text='<title>Acme</title><a href="/team/openings">View careers</a>',
            )
        if request.url.path == "/team/openings":
            return httpx.Response(
                200,
                text=(
                    "<h1>Acme Open Roles</h1>"
                    '<a href="https://job-boards.greenhouse.io/acme">Apply</a>'
                ),
            )
        return httpx.Response(404)

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(
            raw_response(
                "https://acme.test https://www.acme.test/landing?utm_source=search",
                queries=["Acme official"],
            )
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
        max_pages_per_domain=2,
    )

    result = resolver.resolve(company_name="Acme", board_token="acme")

    assert result.status == "accepted"
    assert requested_paths == ["/", "/landing/", "/team/openings"]
    assert len(result.candidate_evidence[0].pages) == 2


def test_redirect_final_domain_is_proposed_without_becoming_proof(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "old.test":
            return httpx.Response(302, headers={"Location": "https://newco.test/missing"})
        return httpx.Response(404)

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(raw_response("old.test", queries=["Newco official"])),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
        max_attempts=1,
    )

    result = resolver.resolve(company_name="Newco", board_token="newco")
    by_domain = {evidence.domain: evidence for evidence in result.candidate_evidence}

    assert result.status == "unresolved"
    assert "newco.test" in by_domain
    assert "page_redirect" in by_domain["newco.test"].candidate_sources
    assert by_domain["newco.test"].brand_valid is False
    assert by_domain["newco.test"].reciprocal_link_valid is False


def test_oversized_page_prefix_is_parsed_instead_of_discarded(tmp_path: Path) -> None:
    proof = (
        "<title>Acme Careers</title>"
        '<a href="https://job-boards.greenhouse.io/acme">Jobs</a>'
    ).encode()
    oversized = proof + b"x" * (MAX_PAGE_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized, headers={"Content-Type": "text/html"})

    resolver = GoogleDomainResolver(
        tmp_path / "responses.json",
        project="test-project",
        client=FakeClient(raw_response("acme.test", queries=["Acme official"])),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
        delay_seconds=0,
    )

    result = resolver.resolve(company_name="Acme", board_token="acme")

    assert result.status == "accepted"
    assert result.candidate_evidence[0].pages[0].error == (
        f"response_truncated:{MAX_PAGE_BYTES}"
    )
