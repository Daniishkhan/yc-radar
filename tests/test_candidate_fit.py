from yc_radar.domain.models import Company
from yc_radar.services.candidate_fit import (
    DEFAULT_CANDIDATE_PROFILE,
    classify_remote_eligibility,
    classify_role_text,
    rank_companies,
    rerank_verified_targets,
    role_focus_record,
    score_company,
    target_record,
)


def test_role_classifier_strong_for_senior_backend_and_infra_software_roles() -> None:
    assert classify_role_text("Senior Backend Engineer").status == "strong"
    assert classify_role_text("Back End Engineer").status == "strong"
    assert (
        classify_role_text(
            "Senior Software Engineer", "Infrastructure, APIs, distributed systems"
        ).status
        == "strong"
    )


def test_role_classifier_possible_for_backend_heavy_full_stack_and_founding_roles() -> None:
    assert classify_role_text(
        "Founding Engineer", "Build APIs, data pipelines, and backend systems"
    ).status in {"possible", "strong"}
    assert (
        classify_role_text(
            "Full Stack Engineer", "Own backend APIs, integrations, and Postgres"
        ).status
        == "possible"
    )


def test_role_classifier_excludes_non_backend_role_lanes() -> None:
    assert classify_role_text("Frontend Engineer").status == "exclude"
    assert classify_role_text("Product Designer").status == "exclude"
    assert classify_role_text("Sales Lead").status == "exclude"
    assert classify_role_text("Software Engineering Intern").status == "exclude"
    assert classify_role_text("ML Research Scientist").status == "exclude"
    assert classify_role_text("Data Analyst").status == "exclude"
    assert classify_role_text("Chief of Staff", "Own our API platform").status == "exclude"
    assert (
        classify_role_text("Senior Product Manager", "Own backend infrastructure").status
        == "exclude"
    )
    assert classify_role_text("Solutions Engineer", "Build integrations").status == "exclude"


def test_role_classifier_marks_frontend_heavy_full_stack_as_weak() -> None:
    assert classify_role_text("Full Stack Engineer", "React, design systems, CSS").status == "weak"


def test_score_company_rewards_candidate_skill_overlap() -> None:
    company = Company(
        name="AgentOps",
        slug="agentops",
        yc_url="https://www.ycombinator.com/companies/agentops",
        website="https://agentops.example",
        one_liner="AI agents infrastructure for developer teams",
        status="Active",
        team_size=4,
        isHiring=True,
        regions=["Remote"],
        industry="B2B",
        subindustry="B2B -> Infrastructure",
        industries=["B2B", "Infrastructure"],
        tags=["Artificial Intelligence", "Developer Tools", "Open Source"],
        prototype_score=40,
    )

    score = score_company(company, DEFAULT_CANDIDATE_PROFILE)

    assert score.fit_score > 80
    assert "AI/LLM" in score.candidate_strength_matches
    assert "Backend systems" in score.candidate_strength_matches


def test_backend_platform_companies_score_above_frontend_or_generic_ai_companies() -> None:
    backend_company = Company(
        name="BackendOps",
        slug="backendops",
        yc_url="https://www.ycombinator.com/companies/backendops",
        website="https://backendops.example",
        one_liner="Backend infrastructure and APIs for data-heavy operations",
        status="Active",
        team_size=8,
        isHiring=True,
        tags=["Infrastructure", "Developer Tools"],
        prototype_score=20,
    )
    frontend_company = Company(
        name="PixelUI",
        slug="pixelui",
        yc_url="https://www.ycombinator.com/companies/pixelui",
        website="https://pixelui.example",
        one_liner="Frontend design tools for React teams",
        status="Active",
        team_size=8,
        isHiring=True,
        tags=["Design Tools"],
        prototype_score=20,
    )
    generic_ai_company = Company(
        name="PromptBox",
        slug="promptbox",
        yc_url="https://www.ycombinator.com/companies/promptbox",
        website="https://promptbox.example",
        one_liner="AI prompt workspace for teams",
        status="Active",
        team_size=8,
        isHiring=True,
        tags=["Artificial Intelligence"],
        prototype_score=20,
    )

    backend_score = score_company(backend_company, DEFAULT_CANDIDATE_PROFILE).fit_score
    frontend_score = score_company(frontend_company, DEFAULT_CANDIDATE_PROFILE).fit_score
    generic_ai_score = score_company(generic_ai_company, DEFAULT_CANDIDATE_PROFILE).fit_score

    assert backend_score > frontend_score
    assert backend_score > generic_ai_score


def test_ai_and_data_signals_boost_backend_relevance_without_becoming_primary_lane() -> None:
    backend_company = Company(
        name="AgentInfra",
        slug="agentinfra",
        yc_url="https://www.ycombinator.com/companies/agentinfra",
        website="https://agentinfra.example",
        one_liner="Backend platform for LLM agents and data pipelines",
        status="Active",
        team_size=6,
        isHiring=True,
        tags=["Artificial Intelligence", "Infrastructure", "Data"],
        prototype_score=20,
    )

    score = score_company(backend_company, DEFAULT_CANDIDATE_PROFILE)
    role_focus = role_focus_record(
        backend_company,
        yc_jobs=[
            {
                "title": "Senior Backend Engineer",
                "skills": ["Python", "LLMs", "Data pipelines"],
            }
        ],
    )

    assert {"Backend systems", "AI/LLM", "Data engineering"}.issubset(
        set(score.candidate_strength_matches)
    )
    assert role_focus["target_role_lane"] == "Senior Backend / Senior Software"
    assert role_focus["role_match_status"] == "strong"


def test_target_record_uses_yc_is_hiring_and_live_unknown_defaults() -> None:
    company = Company(
        name="SmallCo",
        slug="smallco",
        yc_url="https://www.ycombinator.com/companies/smallco",
        website="https://smallco.example",
        one_liner="Data pipelines for AI teams",
        status="Active",
        team_size=5,
        isHiring=True,
    )
    score = rank_companies([company], DEFAULT_CANDIDATE_PROFILE)[0]

    target = target_record(score, rank=1)

    assert target["yc_is_hiring"] is True
    assert target["verified_hiring_status"] == "unknown"
    assert target["verified_roles"] == []
    assert target["firecrawl_pages_used"] == 0
    assert target["target_role_lane"] in {
        "Backend-heavy SWE / Founding Engineer",
        "Unclear backend/SWE fit",
    }
    assert "application_angle" in target


def test_non_yc_company_with_unknown_team_size_remains_in_candidate_pool() -> None:
    company = Company(
        name="Independent Backend Co",
        slug="independent-backend-co",
        website="https://independent.example",
        one_liner="Backend infrastructure for high-volume API teams",
    )

    ranked = rank_companies(
        [company],
        DEFAULT_CANDIDATE_PROFILE,
        max_team_size=25,
    )

    assert [score.company.slug for score in ranked] == ["independent-backend-co"]
    assert "Active YC company" not in ranked[0].fit_reasons


def test_rerank_verified_targets_boosts_live_role_fit() -> None:
    targets = [
        {"rank": 1, "fit_score": 50, "verified_hiring_status": "unknown", "role_fit": "unknown"},
        {"rank": 2, "fit_score": 42, "verified_hiring_status": "hiring", "role_fit": "strong"},
    ]

    reranked = rerank_verified_targets(targets)

    assert reranked[0]["fit_score"] == 74
    assert reranked[0]["rank"] == 1


def test_remote_eligibility_distinguishes_global_pakistan_and_restricted_roles() -> None:
    assert (
        classify_remote_eligibility(
            {"location": "Remote", "description_text": "Work from anywhere in the world."}
        ).status
        == "global_explicit"
    )
    assert (
        classify_remote_eligibility(
            {
                "location": "Remote",
                "description_text": "This remote role is open to candidates based in Pakistan.",
            }
        ).status
        == "pakistan_explicit"
    )
    assert (
        classify_remote_eligibility({"location": "Remote - APAC"}).status
        == "regional_unconfirmed"
    )
    assert (
        classify_remote_eligibility({"location": "Remote - United States"}).status
        == "restricted_remote"
    )
    assert (
        classify_remote_eligibility(
            {
                "location": "San Francisco, CA",
                "description_text": "We are remote-first and have teams across APAC.",
            }
        ).status
        == "no_remote_evidence"
    )
    assert (
        classify_remote_eligibility(
            {
                "location": "Santiago, Chile",
                "description_text": "This position is remote for candidates based in Chile.",
            }
        ).status
        == "restricted_remote"
    )


def test_remote_eligibility_accepts_only_unambiguous_global_location_labels() -> None:
    for location in (
        "World Wide - Remote",
        "Worldwide Remote",
        "Global - Remote Work",
        "Remote - Anywhere",
    ):
        assert classify_remote_eligibility({"location": location}).status == "global_explicit"

    assert classify_remote_eligibility({"location": "Remote"}).status == "remote_unclear"
    assert (
        classify_remote_eligibility({"location": "Remote - APAC"}).status
        == "regional_unconfirmed"
    )


def test_structured_remote_restrictions_override_global_description_boilerplate() -> None:
    global_boilerplate = "We are a global team, and employees can work from anywhere."

    for location in (
        "Remote - United States",
        "Remote-Germany",
        "Remote - Anywhere within the UK",
        "Canada - Remote",
    ):
        assert (
            classify_remote_eligibility(
                {"location": location, "description_text": global_boilerplate}
            ).status
            == "restricted_remote"
        )

    assert (
        classify_remote_eligibility(
            {"location": "Remote", "description_text": global_boilerplate}
        ).status
        == "global_explicit"
    )


def test_remote_restrictions_and_negations_override_global_phrases() -> None:
    cases = (
        {
            "location": "Remote",
            "description_text": "Work from anywhere in the Contiguous US.",
        },
        {
            "location": "Remote - APAC",
            "description_text": "Open across APAC, excluding Pakistan.",
        },
        {
            "location": "Remote",
            "description_text": "This role is remote, but not worldwide.",
        },
        {
            "location": "Remote",
            "description_text": (
                "Our global team can work from anywhere. Candidates must reside in Canada."
            ),
        },
    )

    for job in cases:
        result = classify_remote_eligibility(job)
        assert result.status == "restricted_remote"
        assert result.evidence


def test_remote_region_mentions_require_role_eligibility_context() -> None:
    assert (
        classify_remote_eligibility(
            {
                "location": "Remote",
                "description_text": "This remote role supports APAC customers and US accounts.",
            }
        ).status
        == "remote_unclear"
    )
    assert (
        classify_remote_eligibility(
            {
                "location": "Remote - APAC",
                "description_text": (
                    "You must be based in Japan, hold JLPT N1, and work Japan business hours."
                ),
            }
        ).status
        == "restricted_remote"
    )
    assert (
        classify_remote_eligibility(
            {
                "location": "Remote",
                "description_text": (
                    "This is a remote opportunity requiring Japan business hours."
                ),
            }
        ).status
        == "regional_unconfirmed"
    )


def test_uncommon_remote_wording_and_no_evidence_are_distinct_from_onsite() -> None:
    for description in (
        "This is a remote opportunity.",
        "This role may be performed remotely.",
        "This is a fully distributed position.",
        "This is a location-flexible role.",
    ):
        assert (
            classify_remote_eligibility({"description_text": description}).status
            == "remote_unclear"
        )

    assert (
        classify_remote_eligibility(
            {"location": "Karachi", "description_text": "This role is onsite five days a week."}
        ).status
        == "onsite_explicit"
    )
    assert (
        classify_remote_eligibility({"location": "San Francisco, CA"}).status
        == "no_remote_evidence"
    )


def test_structured_provider_evidence_takes_precedence_over_boilerplate() -> None:
    def structured_job(
        workplace_type: str,
        countries: list[str],
        *,
        description: str = "",
        eligibility_signals: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            "description_text": description,
            "structured_evidence": {
                "schema_version": 1,
                "provider": "ashby",
                "workplace": {
                    "type": workplace_type,
                    "is_remote": workplace_type == "remote",
                },
                "primary_location": None,
                "secondary_locations": [],
                "offices": [],
                "countries": countries,
                "provider_metadata": [],
                "eligibility_signals": eligibility_signals or [],
                "application": {},
            },
        }

    # Provider countries are posting-address evidence, not applicant eligibility proof.
    assert (
        classify_remote_eligibility(structured_job("remote", ["Pakistan"])).status
        == "remote_unclear"
    )
    assert (
        classify_remote_eligibility(structured_job("remote", ["Worldwide"])).status
        == "remote_unclear"
    )
    assert classify_remote_eligibility(
        structured_job(
            "remote",
            ["Pakistan"],
            eligibility_signals=[
                {"kind": "provider_metadata", "name": "Eligible countries", "value": "Pakistan"}
            ],
        )
    ).status == "pakistan_explicit"
    assert classify_remote_eligibility(
        structured_job(
            "remote",
            [],
            eligibility_signals=[
                {"kind": "provider_metadata", "name": "Eligible countries", "value": "Worldwide"}
            ],
        )
    ).status == "global_explicit"
    assert classify_remote_eligibility(structured_job("remote", ["United States"])).status == (
        "restricted_remote"
    )
    assert classify_remote_eligibility(structured_job("remote", [])).status == "remote_unclear"
    assert classify_remote_eligibility(
        structured_job("on_site", [], description="Employees can work from anywhere.")
    ).status == "onsite_explicit"


def test_role_focus_clusters_location_variants_without_losing_provenance() -> None:
    company = Company(
        name="Location Fanout Co",
        slug="location-fanout-co",
        website="https://location-fanout.example",
    )
    jobs = [
        {
            "title": "Senior Backend Engineer",
            "location": "Remote - United States",
            "description_text": "Build distributed APIs.",
            "provider": "greenhouse",
            "external_job_id": "us-1",
        },
        {
            "title": "Senior Backend Engineer",
            "location": "Remote - Worldwide",
            "description_text": "Build distributed APIs.",
            "provider": "greenhouse",
            "external_job_id": "world-1",
        },
        {
            "title": "Staff Platform Engineer",
            "location": "Remote",
            "description_text": "Own the data platform.",
            "provider": "greenhouse",
            "external_job_id": "platform-1",
        },
    ]

    target = target_record(
        score_company(company, DEFAULT_CANDIDATE_PROFILE),
        rank=1,
        canonical_jobs=jobs,
    )

    assert target["canonical_raw_active_job_count"] == 3
    assert target["canonical_active_job_count"] == 2
    assert target["canonical_duplicate_posting_count"] == 1
    assert target["canonical_raw_matching_job_count"] == 3
    assert target["canonical_matching_job_count"] == 2
    assert target["canonical_duplicate_matching_job_count"] == 1
    assert target["best_remote_eligibility"] == "global_explicit"

    backend_cluster = next(
        item
        for item in target["matching_job_provenance"]
        if item["title"] == "Senior Backend Engineer"
    )
    assert backend_cluster["posting_variant_count"] == 2
    assert backend_cluster["remote_eligibility"] == "global_explicit"
    assert backend_cluster["remote_eligibility_distribution"] == {
        "restricted_remote": 1,
        "global_explicit": 1,
    }
    assert {
        (item["external_job_id"], item["location"], item["remote_eligibility"])
        for item in backend_cluster["posting_variants"]
    } == {
        ("us-1", "Remote - United States", "restricted_remote"),
        ("world-1", "Remote - Worldwide", "global_explicit"),
    }


def test_live_global_remote_backend_role_can_outrank_metadata_rich_company_without_roles() -> None:
    independent = Company(
        name="Independent Systems",
        slug="independent-systems",
        website="https://independent.example",
    )
    metadata_rich = Company(
        name="Metadata Rich",
        slug="metadata-rich",
        yc_url="https://www.ycombinator.com/companies/metadata-rich",
        website="https://metadata-rich.example",
        one_liner="AI infrastructure",
        status="Active",
        team_size=5,
        isHiring=True,
        prototype_score=40,
    )
    independent_target = target_record(
        score_company(independent, DEFAULT_CANDIDATE_PROFILE),
        rank=1,
        canonical_jobs=[
            {
                "title": "Senior Backend Engineer",
                "location": "Remote - Worldwide",
                "description_text": "Build distributed APIs. Remote worldwide.",
                "provider": "greenhouse",
                "external_job_id": "1",
            }
        ],
    )
    metadata_target = target_record(
        score_company(metadata_rich, DEFAULT_CANDIDATE_PROFILE),
        rank=2,
        canonical_jobs=[],
    )

    assert independent_target["best_remote_eligibility"] == "global_explicit"
    assert independent_target["opportunity_score"] > 80
    assert independent_target["fit_score"] > metadata_target["fit_score"]
