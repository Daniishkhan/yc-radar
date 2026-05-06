from yc_radar.domain.models import Company
from yc_radar.services.candidate_fit import (
    DEFAULT_CANDIDATE_PROFILE,
    rank_companies,
    rerank_verified_targets,
    score_company,
    target_record,
)


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


def test_rerank_verified_targets_boosts_live_role_fit() -> None:
    targets = [
        {"rank": 1, "fit_score": 50, "verified_hiring_status": "unknown", "role_fit": "unknown"},
        {"rank": 2, "fit_score": 42, "verified_hiring_status": "hiring", "role_fit": "strong"},
    ]

    reranked = rerank_verified_targets(targets)

    assert reranked[0]["fit_score"] == 74
    assert reranked[0]["rank"] == 1
