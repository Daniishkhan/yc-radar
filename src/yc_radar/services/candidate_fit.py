from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yc_radar.agents.llm import LLMClient
from yc_radar.domain.models import Company


DEFAULT_CANDIDATE_PROFILE: dict[str, Any] = {
    "headline": "Senior Software Engineer | AI, Backend, Data Engineering, Full Stack",
    "summary": (
        "Senior AI/backend/data engineer with experience building LLM-powered products, "
        "backend systems, data pipelines, and full-stack software."
    ),
    "target_roles": [
        "Senior AI Engineer",
        "Senior Backend Engineer",
        "Senior Full-Stack Engineer",
        "AI Infrastructure Engineer",
        "Data Engineering Lead",
        "Founding Engineer",
    ],
    "core_expertise": [
        "Large language models",
        "AI engineering",
        "Backend systems",
        "Data engineering",
        "Full-stack product engineering",
        "Distributed systems",
        "ETL and high-volume ingestion",
        "Evaluation benchmarks",
        "Agentic coding workflows",
    ],
    "technical_skills": {
        "languages": ["Python", "TypeScript", "JavaScript"],
        "backend": ["Node.js", "FastAPI", "REST APIs", "event-driven architecture"],
        "frontend": ["React", "Next.js"],
        "ai_llm": ["OpenAI", "LangChain", "LLM evaluation", "autonomous coding agents"],
        "data": ["ETL pipelines", "large-scale data ingestion", "data warehouses"],
        "cloud_infra": ["AWS", "Azure", "GCP", "Docker", "Kubernetes"],
    },
}

SIGNAL_GROUPS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "AI/LLM",
        14,
        (
            "ai",
            "artificial intelligence",
            "llm",
            "large language model",
            "agent",
            "agents",
            "openai",
            "mcp",
            "machine learning",
        ),
    ),
    (
        "Backend systems",
        10,
        (
            "backend",
            "infrastructure",
            "platform",
            "api",
            "developer tools",
            "devtools",
            "cloud",
            "distributed",
        ),
    ),
    (
        "Data engineering",
        9,
        (
            "data",
            "etl",
            "pipeline",
            "warehouse",
            "analytics",
            "ingestion",
            "observability",
        ),
    ),
    (
        "Full-stack product",
        7,
        ("full-stack", "full stack", "react", "next.js", "typescript", "product engineer"),
    ),
    (
        "Open-source leverage",
        7,
        ("open source", "open-source", "github", "sdk", "framework", "library"),
    ),
    (
        "Workflow automation",
        6,
        ("automation", "workflow", "operations", "copilot", "assistant", "integration"),
    ),
)

REGION_MATCH_TERMS = ("remote", "pakistan", "india", "asia", "south asia", "mena", "middle east")


@dataclass(frozen=True)
class CandidateScore:
    company: Company
    fit_score: int
    fit_reasons: list[str]
    candidate_strength_matches: list[str]


def load_candidate_profile(profile_path: Path) -> dict[str, Any]:
    if not profile_path.exists():
        return DEFAULT_CANDIDATE_PROFILE
    return json.loads(profile_path.read_text(encoding="utf-8"))


def profile_text(profile: dict[str, Any]) -> str:
    chunks: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(profile)
    return " ".join(chunks).lower()


def _company_text(company: Company) -> str:
    parts = [
        company.name,
        company.one_liner,
        company.industry,
        company.subindustry,
        " ".join(company.industries),
        " ".join(company.tags),
        " ".join(company.regions),
        company.all_locations,
        company.prototype_angle,
    ]
    return " ".join(part for part in parts if part).lower()


def _has_signal(text: str, term: str) -> bool:
    if len(term) <= 3 and term.isalpha():
        return re.search(rf"\b{re.escape(term)}\b", text) is not None
    return term in text


def score_company(company: Company, profile: dict[str, Any]) -> CandidateScore:
    score = min(company.prototype_score or 0, 50)
    reasons: list[str] = []
    matches: list[str] = []
    company_text = _company_text(company)
    candidate_text = profile_text(profile)

    if score:
        reasons.append(f"Existing prototype score {score}")

    if (company.status or "").lower() == "active":
        score += 8
        reasons.append("Active YC company")

    if company.website:
        score += 4
        reasons.append("Has a live website to inspect")

    if company.is_hiring:
        score += 8
        reasons.append("YC marks the company as hiring")

    if company.team_size is not None:
        if company.team_size <= 5:
            score += 14
            reasons.append("Tiny team where a strong prototype can stand out")
        elif company.team_size <= 10:
            score += 10
            reasons.append("Small team with likely founder access")
        elif company.team_size <= 25:
            score += 6
            reasons.append("Small enough for direct senior-engineer outreach")
        elif company.team_size > 75:
            score -= 8
            reasons.append("Larger team makes prototype outreach less asymmetric")

    if any(term in company_text for term in REGION_MATCH_TERMS):
        score += 4
        reasons.append("Location looks remote/APAC-friendly enough to try")

    for label, weight, terms in SIGNAL_GROUPS:
        if any(_has_signal(company_text, term) for term in terms) and any(
            _has_signal(candidate_text, term) for term in terms
        ):
            score += weight
            matches.append(label)

    if company.batch and any(year in company.batch for year in ("2024", "2025", "2026")):
        score += 5
        reasons.append("Recent YC batch likely still founder-led")

    if not matches:
        reasons.append("General senior engineering fit, but no sharp skill overlap signal")

    return CandidateScore(
        company=company,
        fit_score=max(score, 0),
        fit_reasons=reasons[:7],
        candidate_strength_matches=matches[:6],
    )


def rank_companies(
    companies: list[Company],
    profile: dict[str, Any],
    *,
    max_team_size: int | None = 25,
    require_website: bool = True,
) -> list[CandidateScore]:
    scored: list[CandidateScore] = []
    for company in companies:
        if require_website and not company.website:
            continue
        if max_team_size is not None:
            if company.team_size is None or company.team_size > max_team_size:
                continue
        scored.append(score_company(company, profile))
    return sorted(scored, key=lambda item: item.fit_score, reverse=True)


def target_record(score: CandidateScore, *, rank: int) -> dict[str, Any]:
    company = score.company
    return {
        "rank": rank,
        "id": company.id,
        "name": company.name,
        "slug": company.slug,
        "yc_url": company.yc_url,
        "website": company.website,
        "one_liner": company.one_liner,
        "batch": company.batch,
        "status": company.status,
        "stage": company.stage,
        "team_size": company.team_size,
        "yc_is_hiring": company.is_hiring,
        "all_locations": company.all_locations,
        "regions": company.regions,
        "industry": company.industry,
        "subindustry": company.subindustry,
        "industries": company.industries,
        "tags": company.tags,
        "prototype_score": company.prototype_score,
        "prototype_angle": company.prototype_angle,
        "fit_score": score.fit_score,
        "fit_reasons": score.fit_reasons,
        "candidate_strength_matches": score.candidate_strength_matches,
        "verified_hiring_status": "unknown",
        "career_page_url": None,
        "verified_roles": [],
        "role_fit": "unknown",
        "verification_source_url": None,
        "verification_checked_at": None,
        "verification_confidence": 0.0,
        "firecrawl_pages_used": 0,
        "llm_used": False,
        "why_you_fit": "",
        "why_they_might_care": "",
        "prototype_idea": "",
        "best_playbook": score.company.prototype_angle or "",
        "risks": [],
        "next_action": "",
    }


def rerank_verified_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for target in targets:
        score = int(target.get("fit_score") or 0)
        status = target.get("verified_hiring_status")
        role_fit = target.get("role_fit")
        if status == "hiring":
            score += 20
        elif status == "not_hiring":
            score -= 16
        if role_fit == "strong":
            score += 12
        elif role_fit == "possible":
            score += 5
        target["fit_score"] = max(score, 0)
    reranked = sorted(targets, key=lambda item: int(item.get("fit_score") or 0), reverse=True)
    for index, target in enumerate(reranked, start=1):
        target["rank"] = index
    return reranked


def weekly_target_schema() -> dict[str, Any]:
    target_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "slug": {"type": "string"},
            "why_you_fit": {"type": "string"},
            "why_they_might_care": {"type": "string"},
            "prototype_idea": {"type": "string"},
            "best_playbook": {"type": "string"},
            "risks": {"type": "array", "items": {"type": "string"}},
            "next_action": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": [
            "slug",
            "why_you_fit",
            "why_they_might_care",
            "prototype_idea",
            "best_playbook",
            "risks",
            "next_action",
            "confidence",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"targets": {"type": "array", "items": target_item}},
        "required": ["targets"],
    }


async def enrich_targets_with_llm(
    targets: list[dict[str, Any]],
    profile: dict[str, Any],
    llm: LLMClient,
    *,
    batch_size: int = 10,
) -> None:
    system = (
        "You help a senior AI/backend/data engineer choose YC companies to approach. "
        "Be specific, pragmatic, and prototype-first. Do not invent private facts, founders, "
        "emails, or claims that a prototype already exists."
    )
    schema = weekly_target_schema()
    for start in range(0, len(targets), batch_size):
        batch = targets[start : start + batch_size]
        user = json.dumps(
            {
                "candidate_profile": {
                    "headline": profile.get("headline"),
                    "summary": profile.get("summary"),
                    "target_roles": profile.get("target_roles"),
                    "core_expertise": profile.get("core_expertise"),
                    "technical_skills": profile.get("technical_skills"),
                    "positioning": profile.get("positioning"),
                },
                "targets": [
                    {
                        "slug": target["slug"],
                        "name": target["name"],
                        "one_liner": target["one_liner"],
                        "website": target["website"],
                        "team_size": target["team_size"],
                        "yc_is_hiring": target["yc_is_hiring"],
                        "verified_hiring_status": target["verified_hiring_status"],
                        "verified_roles": target["verified_roles"],
                        "candidate_strength_matches": target["candidate_strength_matches"],
                        "fit_reasons": target["fit_reasons"],
                        "prototype_angle": target["prototype_angle"],
                    }
                    for target in batch
                ],
                "instructions": (
                    "For each target, propose one concrete few-hour prototype or PR-style play. "
                    "Keep every string concise. Return exactly one item per slug."
                ),
            },
            indent=2,
        )
        try:
            parsed = await llm.complete_json(
                system=system,
                user=user,
                schema=schema,
                name="weekly_target_enrichment",
            )
        except Exception as exc:
            for target in batch:
                target["llm_error"] = str(exc)
            continue

        by_slug = {item.get("slug"): item for item in parsed.get("targets", [])}
        for target in batch:
            enrichment = by_slug.get(target["slug"])
            if not enrichment:
                continue
            target.update(
                {
                    "llm_used": True,
                    "why_you_fit": enrichment["why_you_fit"],
                    "why_they_might_care": enrichment["why_they_might_care"],
                    "prototype_idea": enrichment["prototype_idea"],
                    "best_playbook": enrichment["best_playbook"],
                    "risks": enrichment["risks"],
                    "next_action": enrichment["next_action"],
                    "llm_confidence": enrichment["confidence"],
                }
            )
