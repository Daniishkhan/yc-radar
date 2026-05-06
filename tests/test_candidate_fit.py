from yc_radar.domain.models import Company
from yc_radar.services.candidate_fit import (
    DEFAULT_CANDIDATE_PROFILE,
    classify_role_text,
    rank_companies,
    rerank_verified_targets,
    role_focus_record,
    score_company,
    target_record,
)


def test_role_classifier_strong_for_senior_backend_and_infra_software_roles() -> None:
    assert classify_role_text("Senior Backend Engineer").status == "strong"
    assert (
        classify_role_text("Senior Software Engineer", "Infrastructure, APIs, distributed systems")
        .status
        == "strong"
    )


def test_role_classifier_possible_for_backend_heavy_full_stack_and_founding_roles() -> None:
    assert (
        classify_role_text("Founding Engineer", "Build APIs, data pipelines, and backend systems")
        .status
        in {"possible", "strong"}
    )
    assert (
        classify_role_text("Full Stack Engineer", "Own backend APIs, integrations, and Postgres")
        .status
        == "possible"
    )


def test_role_classifier_excludes_non_backend_role_lanes() -> None:
    assert classify_role_text("Frontend Engineer").status == "exclude"
    assert classify_role_text("Product Designer").status == "exclude"
    assert classify_role_text("Sales Lead").status == "exclude"
    assert classify_role_text("Software Engineering Intern").status == "exclude"
    assert classify_role_text("ML Research Scientist").status == "exclude"
    assert classify_role_text("Data Analyst").status == "exclude"


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


def test_rerank_verified_targets_boosts_live_role_fit() -> None:
    targets = [
        {"rank": 1, "fit_score": 50, "verified_hiring_status": "unknown", "role_fit": "unknown"},
        {"rank": 2, "fit_score": 42, "verified_hiring_status": "hiring", "role_fit": "strong"},
    ]

    reranked = rerank_verified_targets(targets)

    assert reranked[0]["fit_score"] == 74
    assert reranked[0]["rank"] == 1
