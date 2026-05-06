from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yc_radar.agents.llm import LLMClient
from yc_radar.domain.models import Company


DEFAULT_CANDIDATE_PROFILE: dict[str, Any] = {
    "headline": "Senior Backend / Senior Software Engineer | AI and Data Systems",
    "summary": (
        "Senior backend/software engineer with experience building backend systems, "
        "data pipelines, LLM-powered products, and full-stack software."
    ),
    "target_roles": [
        "Senior Backend Engineer",
        "Senior Software Engineer",
        "Backend Platform Engineer",
        "Infrastructure Engineer",
        "Backend-heavy Founding Engineer",
        "Backend-heavy Full Stack Engineer",
    ],
    "supporting_strengths": [
        "AI engineering",
        "Large language models",
        "Data engineering",
        "Full-stack product delivery",
    ],
    "core_expertise": [
        "Backend systems",
        "Distributed systems",
        "Data engineering",
        "ETL and high-volume ingestion",
        "Large language models",
        "AI engineering",
        "Evaluation benchmarks",
        "Full-stack product engineering",
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

ROLE_STATUS_SCORE_ADJUSTMENTS = {
    "strong": 16,
    "possible": 7,
    "weak": -6,
    "exclude": -30,
}

SENIOR_TERMS = ("senior", "sr", "staff", "principal", "lead")
BACKEND_ROLE_TERMS = (
    "backend",
    "back end",
    "back-end",
    "api",
    "apis",
    "server",
    "server-side",
    "platform",
    "infrastructure",
    "infra",
    "distributed",
    "cloud",
    "database",
    "data platform",
    "data infrastructure",
    "etl",
    "pipeline",
    "pipelines",
    "integration",
    "integrations",
    "python",
    "node",
    "fastapi",
)
SOFTWARE_ROLE_TERMS = (
    "software engineer",
    "software engineering",
    "software developer",
    "swe",
)
FULL_STACK_TERMS = ("full stack", "full-stack", "fullstack")
FOUNDING_TERMS = ("founding engineer", "founding software engineer")
FRONTEND_TERMS = (
    "frontend",
    "front end",
    "front-end",
    "ui engineer",
    "web engineer",
    "react engineer",
    "design engineer",
)
EXCLUDED_ROLE_TERMS = (
    "designer",
    "product designer",
    "sales",
    "account executive",
    "marketing",
    "growth",
    "customer success",
    "support",
    "operations",
    "intern",
    "internship",
    "apprentice",
)
RESEARCH_ONLY_TERMS = (
    "research scientist",
    "ml researcher",
    "ai researcher",
    "machine learning researcher",
)
DATA_ANALYST_TERMS = ("data analyst", "business analyst", "analytics analyst")


@dataclass(frozen=True)
class RoleClassification:
    status: str
    reasons: list[str]


SIGNAL_GROUPS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "Backend systems",
        16,
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
        "AI/LLM",
        7,
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
        3,
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


def _has_any_signal(text: str, terms: tuple[str, ...]) -> bool:
    return any(_has_signal(text, term) for term in terms)


def classify_role_text(title: str, context: str = "") -> RoleClassification:
    title_text = title.lower()
    combined = f"{title} {context}".lower()
    is_full_stack = _has_any_signal(combined, FULL_STACK_TERMS)
    has_backend_signal = _has_any_signal(combined, BACKEND_ROLE_TERMS)
    has_software_signal = _has_any_signal(title_text, SOFTWARE_ROLE_TERMS)
    has_senior_signal = _has_any_signal(title_text, SENIOR_TERMS)
    is_founding = _has_any_signal(title_text, FOUNDING_TERMS)
    has_frontend_signal = _has_any_signal(combined, FRONTEND_TERMS)

    if _has_any_signal(title_text, EXCLUDED_ROLE_TERMS):
        return RoleClassification("exclude", ["Non-engineering or junior/intern role"])
    if _has_any_signal(title_text, DATA_ANALYST_TERMS):
        return RoleClassification("exclude", ["Data analyst role is outside backend/SWE focus"])
    if _has_any_signal(title_text, RESEARCH_ONLY_TERMS) and not has_backend_signal:
        return RoleClassification(
            "exclude", ["Research-only ML role lacks backend/platform signal"]
        )
    if has_frontend_signal and not is_full_stack and not has_backend_signal:
        return RoleClassification("exclude", ["Frontend-only role is outside backend/SWE focus"])

    if (
        not is_full_stack
        and has_backend_signal
        and (
            has_senior_signal
            or is_founding
            or "backend" in title_text
            or "back end" in title_text
            or "back-end" in title_text
            or "platform" in title_text
            or "infrastructure" in title_text
        )
    ):
        return RoleClassification("strong", ["Backend/platform role matches primary target lane"])
    if has_senior_signal and has_software_signal and not has_frontend_signal:
        return RoleClassification(
            "strong", ["Senior software engineering role matches target lane"]
        )
    if is_full_stack and has_backend_signal:
        return RoleClassification(
            "possible", ["Full-stack role has backend/API/data/infra signals"]
        )
    if is_founding and has_backend_signal:
        return RoleClassification("possible", ["Founding role has backend-heavy signals"])
    if is_founding or is_full_stack or has_software_signal:
        return RoleClassification("weak", ["Engineering role exists, but backend depth is unclear"])
    return RoleClassification("weak", ["No clear senior backend/SWE role signal"])


def _job_context(job: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in (
        "title",
        "role",
        "role_specific_type",
        "pretty_role",
        "type",
        "location",
        "visa",
        "salary_range",
        "equity_range",
    ):
        value = job.get(key)
        if value:
            chunks.append(str(value))
    skills = job.get("skills") or []
    if isinstance(skills, list):
        chunks.extend(str(skill) for skill in skills)
    return " ".join(chunks)


def _best_status(classifications: list[tuple[str, RoleClassification]]) -> str:
    if any(classification.status == "strong" for _, classification in classifications):
        return "strong"
    if any(classification.status == "possible" for _, classification in classifications):
        return "possible"
    if any(classification.status == "weak" for _, classification in classifications):
        return "weak"
    return "exclude"


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def role_focus_record(
    company: Company,
    *,
    yc_jobs: list[dict[str, Any]] | None = None,
    verified_roles: list[str] | None = None,
) -> dict[str, Any]:
    role_inputs: list[tuple[str, str]] = []
    for job in yc_jobs or []:
        title = str(job.get("title") or "").strip()
        if title:
            role_inputs.append((title, _job_context(job)))
    for role in verified_roles or []:
        title = str(role).strip()
        if title:
            role_inputs.append((title, title))

    classifications = [
        (title, classify_role_text(title, context)) for title, context in role_inputs
    ]
    matching_titles = [
        title
        for title, classification in classifications
        if classification.status in {"strong", "possible"}
    ]
    reasons = [
        reason
        for _, classification in classifications
        for reason in classification.reasons
        if classification.status in {"strong", "possible"}
    ]

    if classifications:
        status = _best_status(classifications)
        if not reasons:
            reasons = [
                reason for _, classification in classifications for reason in classification.reasons
            ]
    else:
        company_text = _company_text(company)
        if _has_any_signal(company_text, BACKEND_ROLE_TERMS):
            status = "possible"
            reasons = ["No public matching role yet, but company signal is backend/platform-heavy"]
        else:
            status = "weak"
            reasons = ["No public backend/SWE role found yet"]

    if status == "strong":
        target_role_lane = "Senior Backend / Senior Software"
        application_angle = (
            "Apply directly as a senior backend/SWE candidate and lead with backend systems, "
            "data pipelines, and AI infrastructure proof points."
        )
    elif status == "possible":
        target_role_lane = "Backend-heavy SWE / Founding Engineer"
        application_angle = (
            "Approach with a backend-heavy demo or integration that proves senior engineering "
            "judgment before asking for a role conversation."
        )
    elif status == "weak":
        target_role_lane = "Unclear backend/SWE fit"
        application_angle = "Keep as research-only until a backend/SWE role or strong backend-heavy product angle appears."
    else:
        target_role_lane = "Outside backend/SWE focus"
        application_angle = "Do not prioritize for the backend/SWE shortlist."

    return {
        "target_role_lane": target_role_lane,
        "matching_job_titles": _dedupe_preserve_order(matching_titles),
        "role_match_status": status,
        "role_match_reasons": _dedupe_preserve_order(reasons)[:5],
        "application_angle": application_angle,
        "proof_points_to_emphasize": proof_points_for_role_status(status),
    }


def proof_points_for_role_status(status: str) -> list[str]:
    if status == "exclude":
        return []
    points = [
        "Senior backend/API ownership",
        "Distributed systems and infrastructure judgment",
        "Data pipelines and high-volume ingestion",
        "LLM/AI systems as backend product leverage",
        "Remote execution with US teams",
    ]
    if status == "possible":
        points.append("Fast prototype/demo shipping for founder-led teams")
    return points


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


def target_record(
    score: CandidateScore,
    *,
    rank: int,
    yc_jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    company = score.company
    record = {
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
    record.update(role_focus_record(company, yc_jobs=yc_jobs))
    return record


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
        score += ROLE_STATUS_SCORE_ADJUSTMENTS.get(str(target.get("role_match_status")), 0)
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
        "You help a senior backend/software engineer choose companies to approach. "
        "Use AI, LLM, and data engineering experience as supporting proof points, not as the "
        "primary role lane. Be specific, pragmatic, and prototype-first. Do not invent private "
        "facts, founders, emails, or claims that a prototype already exists."
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
                        "target_role_lane": target["target_role_lane"],
                        "role_match_status": target["role_match_status"],
                        "role_match_reasons": target["role_match_reasons"],
                        "application_angle": target["application_angle"],
                        "proof_points_to_emphasize": target["proof_points_to_emphasize"],
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
