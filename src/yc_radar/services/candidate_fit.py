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
    "chief of staff",
    "product manager",
    "program manager",
    "project manager",
    "finance",
    "counsel",
    "legal",
    "recruiter",
    "recruiting",
    "talent acquisition",
    "human resources",
    "people partner",
    "account manager",
    "partnerships",
)
EXCLUDED_ENGINEERING_TITLE_TERMS = (
    "sales engineer",
    "solutions engineer",
    "solution engineer",
    "support engineer",
    "customer engineer",
    "field engineer",
    "qa engineer",
    "quality engineer",
    "test engineer",
    "engineering manager",
    "manager, engineering",
    "director of engineering",
    "head of engineering",
    "vp of engineering",
    "vice president of engineering",
    "developer advocate",
    "developer relations",
)
ENGINEERING_TITLE_TERMS = (
    "engineer",
    "developer",
    "architect",
    "site reliability",
    "sre",
    "devops",
    "member of technical staff",
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
REMOTE_GLOBAL_TERMS = (
    "remote worldwide",
    "worldwide remote",
    "remote world wide",
    "world wide remote",
    "work from anywhere",
    "anywhere in the world",
    "globally remote",
    "global remote",
    "remote anywhere",
    "remote - anywhere",
)
REMOTE_PAKISTAN_COMPATIBLE_TERMS = (
    "pakistan",
    "south asia",
    "asia pacific",
    "apac",
)
REMOTE_RESTRICTED_LOCATION_TERMS = (
    "united states",
    "u.s.",
    "usa",
    "canada",
    "north america",
    "latin america",
    "latam",
    "europe",
    "european union",
    "united kingdom",
    "uk only",
    "australia",
    "singapore",
    "india",
    "emea",
    "americas",
)
REMOTE_DESCRIPTION_TERMS = (
    "remote role",
    "remote position",
    "position is remote",
    "role is remote",
    "work remotely",
    "fully remote",
    "can be remote",
    "may be remote",
)
REMOTE_ELIGIBILITY_ORDER = {
    "global_remote": 0,
    "pakistan_compatible": 1,
    "remote_unclear": 2,
    "restricted_remote": 3,
    "not_remote": 4,
}
MAX_MATCHING_JOB_DETAILS = 25


@dataclass(frozen=True)
class CandidateScore:
    company: Company
    fit_score: int
    fit_reasons: list[str]
    candidate_strength_matches: list[str]


@dataclass(frozen=True)
class RemoteEligibility:
    status: str
    reasons: list[str]


_GLOBAL_REMOTE_LOCATION_PATTERNS = (
    r"remote anywhere(?: in (?:the )?world)?",
    r"anywhere(?: in (?:the )?world)? remote",
    r"remote (?:worldwide|world wide)",
    r"(?:worldwide|world wide) remote",
    r"global remote(?: work)?",
    r"remote global(?: work)?",
    r"globally remote(?: work)?",
    r"remote globally(?: work)?",
)
_GENERIC_REMOTE_LOCATION_PATTERNS = (
    r"remote",
    r"fully remote",
    r"remote (?:eligible|first|optional|position|role|work)",
    r"(?:distributed|remote) first",
)


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
    if _has_any_signal(title_text, EXCLUDED_ENGINEERING_TITLE_TERMS):
        return RoleClassification("exclude", ["Engineering-adjacent role is outside the IC SWE lane"])
    if _has_any_signal(title_text, DATA_ANALYST_TERMS):
        return RoleClassification("exclude", ["Data analyst role is outside backend/SWE focus"])
    if _has_any_signal(title_text, RESEARCH_ONLY_TERMS) and not has_backend_signal:
        return RoleClassification(
            "exclude", ["Research-only ML role lacks backend/platform signal"]
        )
    if has_frontend_signal and not is_full_stack and not has_backend_signal:
        return RoleClassification("exclude", ["Frontend-only role is outside backend/SWE focus"])
    if not _has_any_signal(title_text, ENGINEERING_TITLE_TERMS):
        return RoleClassification("exclude", ["Title is not an engineering role"])

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
        "department",
        "employment_type",
        "description_text",
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
    canonical_jobs: list[dict[str, Any]] | None = None,
    verified_roles: list[str] | None = None,
) -> dict[str, Any]:
    role_inputs: list[tuple[str, str, dict[str, Any] | None]] = []
    for job in yc_jobs or []:
        title = str(job.get("title") or "").strip()
        if title:
            role_inputs.append((title, _job_context(job), None))
    for job in canonical_jobs or []:
        title = str(job.get("title") or "").strip()
        if title:
            role_inputs.append((title, _job_context(job), job))
    for role in verified_roles or []:
        title = str(role).strip()
        if title:
            role_inputs.append((title, title, None))

    classifications = [
        (title, classify_role_text(title, context), canonical_job)
        for title, context, canonical_job in role_inputs
    ]
    matching_titles = [
        title
        for title, classification, _ in classifications
        if classification.status in {"strong", "possible"}
    ]
    reasons = [
        reason
        for _, classification, _ in classifications
        for reason in classification.reasons
        if classification.status in {"strong", "possible"}
    ]

    if classifications:
        status = _best_status([(title, classification) for title, classification, _ in classifications])
        if not reasons:
            reasons = [
                reason
                for _, classification, _ in classifications
                for reason in classification.reasons
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

    matching_provenance_all = [
        _canonical_job_provenance(job)
        for title, classification, job in classifications
        if job is not None and classification.status in {"strong", "possible"} and title
    ]
    remote_counts: dict[str, int] = {}
    for job in matching_provenance_all:
        remote_status = str(job["remote_eligibility"])
        remote_counts[remote_status] = remote_counts.get(remote_status, 0) + 1
    best_remote = min(
        remote_counts,
        key=lambda status: REMOTE_ELIGIBILITY_ORDER.get(status, 99),
        default="not_remote",
    )
    matching_provenance = matching_provenance_all[:MAX_MATCHING_JOB_DETAILS]
    canonical_classifications = [
        (title, classification)
        for title, classification, job in classifications
        if job is not None
    ]
    canonical_role_status = (
        _best_status(canonical_classifications) if canonical_classifications else "none"
    )
    return {
        "target_role_lane": target_role_lane,
        "matching_job_titles": _dedupe_preserve_order(matching_titles)[:MAX_MATCHING_JOB_DETAILS],
        "canonical_active_job_count": len(canonical_jobs or []),
        "canonical_matching_job_count": len(matching_provenance_all),
        "canonical_role_match_status": canonical_role_status,
        "canonical_matching_jobs": matching_provenance,
        "matching_job_provenance": matching_provenance,
        "best_remote_eligibility": best_remote,
        "globally_remote_matching_job_count": remote_counts.get("global_remote", 0),
        "pakistan_compatible_matching_job_count": remote_counts.get(
            "pakistan_compatible", 0
        ),
        "remote_matching_job_count": sum(
            remote_counts.get(status, 0)
            for status in ("global_remote", "pakistan_compatible", "remote_unclear")
        ),
        "role_match_status": status,
        "role_match_reasons": _dedupe_preserve_order(reasons)[:5],
        "application_angle": application_angle,
        "proof_points_to_emphasize": proof_points_for_role_status(status),
    }


def _canonical_job_provenance(job: dict[str, Any]) -> dict[str, Any]:
    def iso(value: Any) -> Any:
        return value.isoformat() if hasattr(value, "isoformat") else value

    remote = classify_remote_eligibility(job)
    return {
        "title": job.get("title"),
        "provider": job.get("provider"),
        "external_job_id": str(job.get("external_job_id") or ""),
        "career_source_kind": job.get("career_source_kind"),
        "career_source_url": job.get("career_source_url"),
        "posting_url": job.get("posting_url"),
        "location": job.get("location"),
        "department": job.get("department"),
        "remote_eligibility": remote.status,
        "remote_reasons": remote.reasons,
        "source_published_at": iso(job.get("source_published_at")),
        "source_updated_at": iso(job.get("source_updated_at")),
    }


def classify_remote_eligibility(job: dict[str, Any]) -> RemoteEligibility:
    location = str(job.get("location") or "").lower()
    description = str(job.get("description_text") or "").lower()
    location_is_remote = bool(re.search(r"\bremote\b", location))
    description_is_global = _has_global_remote_claim(description)
    description_is_remote = any(
        term in description for term in REMOTE_DESCRIPTION_TERMS
    ) or description_is_global
    if not location_is_remote and not description_is_remote:
        return RemoteEligibility("not_remote", ["No explicit remote signal"])

    # A structured location is stronger evidence than reusable description copy. In
    # particular, text such as "work from anywhere" must not turn Remote-US or
    # Remote-Germany into a globally eligible role.
    if location_is_remote and "pakistan" in location:
        return RemoteEligibility(
            "pakistan_compatible",
            ["Structured location explicitly includes Pakistan"],
        )
    if _structured_remote_location_is_restricted(location):
        return RemoteEligibility(
            "restricted_remote",
            ["Structured remote location is explicitly geographically restricted"],
        )
    if _is_unambiguously_global_remote_location(location):
        return RemoteEligibility("global_remote", ["Role explicitly allows worldwide remote work"])
    if any(term in location for term in REMOTE_PAKISTAN_COMPATIBLE_TERMS):
        return RemoteEligibility(
            "pakistan_compatible",
            ["Role explicitly includes Pakistan or an APAC/South Asia region"],
        )

    if description_is_remote and location and not location_is_remote:
        return RemoteEligibility(
            "restricted_remote",
            ["Posting pairs remote language with a specific non-Pakistan location"],
        )
    if description_is_global:
        return RemoteEligibility("global_remote", ["Role explicitly allows worldwide remote work"])
    if _remote_region_claim(description, REMOTE_PAKISTAN_COMPATIBLE_TERMS):
        return RemoteEligibility(
            "pakistan_compatible",
            ["Role explicitly includes Pakistan or an APAC/South Asia region"],
        )
    if _remote_region_claim(description, REMOTE_RESTRICTED_LOCATION_TERMS):
        return RemoteEligibility(
            "restricted_remote",
            ["Remote location is explicitly restricted outside Pakistan"],
        )
    return RemoteEligibility(
        "remote_unclear",
        ["Role is remote, but eligible countries are not explicit"],
    )


def _normalise_remote_location(location: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", location.lower())).strip()


def _has_global_remote_claim(text: str) -> bool:
    normalised = _normalise_remote_location(text)
    return any(term in normalised for term in REMOTE_GLOBAL_TERMS)


def _is_unambiguously_global_remote_location(location: str) -> bool:
    normalised = _normalise_remote_location(location)
    return any(re.fullmatch(pattern, normalised) for pattern in _GLOBAL_REMOTE_LOCATION_PATTERNS)


def _structured_remote_location_is_restricted(location: str) -> bool:
    if not re.search(r"\bremote\b", location):
        return False
    if any(term in location for term in REMOTE_RESTRICTED_LOCATION_TERMS):
        return True
    if _is_unambiguously_global_remote_location(location):
        return False
    if any(term in location for term in REMOTE_PAKISTAN_COMPATIBLE_TERMS):
        return False

    normalised = _normalise_remote_location(location)
    return not any(
        re.fullmatch(pattern, normalised) for pattern in _GENERIC_REMOTE_LOCATION_PATTERNS
    )


def _remote_region_claim(text: str, terms: tuple[str, ...]) -> bool:
    claim_terms = ("remote", "based", "located", "reside", "work from", "eligible")
    for region in terms:
        region_pattern = re.escape(region)
        for claim in claim_terms:
            claim_pattern = re.escape(claim)
            if re.search(rf"{claim_pattern}.{{0,80}}{region_pattern}", text) or re.search(
                rf"{region_pattern}.{{0,80}}{claim_pattern}", text
            ):
                return True
    return False


def current_opportunity_score(target: dict[str, Any]) -> tuple[int, list[str]]:
    matching_titles = list(target.get("matching_job_titles") or [])
    active_count = int(target.get("canonical_active_job_count") or 0)
    canonical_matching_count = int(target.get("canonical_matching_job_count") or 0)
    if not canonical_matching_count:
        if active_count:
            return -12, ["Has active jobs, but none match the backend/SWE lane"]
        role_status = str(target.get("role_match_status") or "weak")
        if matching_titles and role_status in ROLE_STATUS_SCORE_ADJUSTMENTS:
            score = ROLE_STATUS_SCORE_ADJUSTMENTS[role_status]
            return score, ["Matching role evidence lacks a complete provider snapshot"]
        return 0, []

    score = 0
    reasons: list[str] = []
    role_status = str(target.get("canonical_role_match_status") or "weak")
    if role_status == "strong":
        score += 70
        reasons.append("Has a current strong senior backend/SWE role")
    elif role_status == "possible":
        score += 40
        reasons.append("Has a current backend-heavy engineering possibility")
    elif role_status == "exclude":
        score -= 30

    score += min(canonical_matching_count, 10) * 2
    if active_count:
        score += 8
        reasons.append("Backed by a complete canonical provider snapshot")

    remote_status = str(target.get("best_remote_eligibility") or "not_remote")
    if remote_status == "global_remote":
        score += 30
        reasons.append("At least one matching role is explicitly worldwide remote")
    elif remote_status == "pakistan_compatible":
        score += 24
        reasons.append("At least one matching role explicitly includes Pakistan/APAC")
    elif remote_status == "remote_unclear":
        score += 10
        reasons.append("At least one matching role is remote with unclear country eligibility")
    elif remote_status == "restricted_remote":
        score -= 8
        reasons.append("Matching remote roles appear geographically restricted")
    return score, reasons


def apply_current_opportunity_score(target: dict[str, Any]) -> None:
    base_score = int(target.get("company_fit_score", target.get("fit_score") or 0))
    opportunity_score, reasons = current_opportunity_score(target)
    target["company_fit_score"] = base_score
    target["opportunity_score"] = opportunity_score
    target["opportunity_score_reasons"] = reasons
    target["fit_score"] = max(base_score + opportunity_score, 0)


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

    if company.yc_url and (company.status or "").lower() == "active":
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
        if (
            max_team_size is not None
            and company.team_size is not None
            and company.team_size > max_team_size
        ):
            continue
        scored.append(score_company(company, profile))
    return sorted(scored, key=lambda item: item.fit_score, reverse=True)


def target_record(
    score: CandidateScore,
    *,
    rank: int,
    yc_jobs: list[dict[str, Any]] | None = None,
    canonical_jobs: list[dict[str, Any]] | None = None,
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
        "company_fit_score": score.fit_score,
        "opportunity_score": 0,
        "opportunity_score_reasons": [],
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
    record.update(role_focus_record(company, yc_jobs=yc_jobs, canonical_jobs=canonical_jobs))
    apply_current_opportunity_score(record)
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
        if "opportunity_score" not in target:
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
