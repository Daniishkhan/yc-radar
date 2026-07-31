from pathlib import Path

import httpx
import yc_radar.services.greenhouse_scout as greenhouse_scout_module

from yc_radar.services.greenhouse_scout import (
    GreenhouseBoardEvidence,
    GreenhouseBoardScout,
    analyze_greenhouse_jobs_payload,
    choose_company_website,
    external_job_origin,
    resolve_company,
)


def test_jobs_payload_yields_verified_company_and_custom_domain() -> None:
    evidence = analyze_greenhouse_jobs_payload(
        "acme",
        {
            "jobs": [
                {
                    "id": 1,
                    "company_name": "Acme",
                    "absolute_url": "https://careers.acme.com/jobs/1?gh_jid=1",
                },
                {
                    "id": 2,
                    "company_name": "Acme",
                    "absolute_url": "https://job-boards.eu.greenhouse.io/acme/jobs/2",
                },
            ],
            "meta": {"total": 2},
        },
    )

    assert evidence.verification_status == "verified"
    assert evidence.company_name == "Acme"
    assert evidence.job_count == 2
    assert evidence.external_job_origins == ("https://careers.acme.com",)
    assert choose_company_website(evidence.external_job_origins) == "https://acme.com"


def test_jobs_payload_rejects_incomplete_or_ambiguous_identity() -> None:
    incomplete = analyze_greenhouse_jobs_payload(
        "acme",
        {"jobs": [{"id": 1, "company_name": "Acme"}], "meta": {"total": 2}},
    )
    ambiguous = analyze_greenhouse_jobs_payload(
        "acme",
        {
            "jobs": [
                {"id": 1, "company_name": "Acme"},
                {"id": 2, "company_name": "Other"},
            ],
            "meta": {"total": 2},
        },
    )

    assert incomplete.verification_status == "invalid"
    assert incomplete.error == "incomplete_jobs_list"
    assert ambiguous.verification_status == "invalid"
    assert ambiguous.error == "missing_or_ambiguous_company_name"


def test_external_job_origin_rejects_ats_and_shared_hosts() -> None:
    assert external_job_origin("https://job-boards.greenhouse.io/acme/jobs/1") is None
    assert external_job_origin("https://job-boards.eu.greenhouse.io/acme/jobs/1") is None
    assert external_job_origin("https://acme.notion.site/job") is None
    assert external_job_origin("https://www.acme.com/jobs/1") == "https://www.acme.com"


def test_resolution_is_fail_closed_on_name_domain_conflict() -> None:
    evidence = GreenhouseBoardEvidence(
        board_token="acme",
        verification_status="verified",
        http_status=200,
        company_name="Acme",
        job_count=2,
        external_job_origins=("https://jobs.other.com",),
    )
    companies = [{"id": 7, "name": "Acme", "primary_domain": "acme.com"}]

    resolution = resolve_company(evidence, companies=companies)

    assert resolution.status == "identity_conflict"
    assert resolution.company_id is None


def test_resolution_allows_unique_exact_name_or_new_custom_domain() -> None:
    existing = GreenhouseBoardEvidence(
        board_token="acme",
        verification_status="verified",
        http_status=200,
        company_name="Acme",
        job_count=1,
        external_job_origins=(),
    )
    new = GreenhouseBoardEvidence(
        board_token="newco",
        verification_status="verified",
        http_status=200,
        company_name="New Co",
        job_count=1,
        external_job_origins=("https://careers.newco.dev",),
    )

    existing_resolution = resolve_company(
        existing,
        companies=[{"id": 7, "name": "Acme", "primary_domain": "acme.com"}],
    )
    new_resolution = resolve_company(new, companies=[])

    assert existing_resolution.status == "existing_exact_name"
    assert existing_resolution.company_id == 7
    assert new_resolution.status == "new_company_domain_candidate"
    assert new_resolution.website_candidate == "https://newco.dev"


def test_scout_caches_public_get_response(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 1,
                        "company_name": "Acme",
                        "absolute_url": "https://acme.com/jobs/1",
                    }
                ],
                "meta": {"total": 1},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with GreenhouseBoardScout(tmp_path / "cache", client=client, delay_seconds=0) as scout:
        first = scout.verify("acme")
        second = scout.verify("acme")

    assert first.verification_status == "verified"
    assert first.cache_source == "network"
    assert second.cache_source == "disk"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert (
        str(requests[0].url)
        == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false"
    )
    assert scout.cache.load(str(requests[0].url))["status_code"] == 200


def test_scout_fails_explicitly_instead_of_parsing_a_truncated_board(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(greenhouse_scout_module, "MAX_SCOUT_TEXT_CHARS", 20)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 1,
                            "company_name": "Large Company",
                            "absolute_url": "https://large.example/jobs/1",
                        }
                    ],
                    "meta": {"total": 1},
                },
            )
        )
    )

    with GreenhouseBoardScout(tmp_path / "cache", client=client, delay_seconds=0) as scout:
        evidence = scout.verify("large")

    assert evidence.verification_status == "failed"
    assert evidence.error is not None
    assert evidence.error.startswith("response_too_large:")


def test_board_page_logo_or_external_redirect_supplies_verified_origin(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 1,
                            "company_name": "Acme",
                            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
                        }
                    ],
                    "meta": {"total": 1},
                },
            )
        return httpx.Response(
            200,
            text=(
                '<main><a class="brand logo" href="https://www.acme.com/careers">'
                "Acme</a></main>"
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with GreenhouseBoardScout(tmp_path / "cache", client=client, delay_seconds=0) as scout:
        evidence = scout.enrich_from_board_page(scout.verify("acme"))

    assert evidence.board_page_origin == "https://acme.com"
    resolution = resolve_company(evidence, companies=[])
    assert resolution.status == "new_company_domain_candidate"
    assert resolution.website_candidate == "https://acme.com"
