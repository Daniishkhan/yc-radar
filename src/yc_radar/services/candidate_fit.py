from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from yc_radar.agents.llm import LLMClient
from yc_radar.domain.models import Company


DEFAULT_CANDIDATE_PROFILE: dict[str, Any] = {
    "headline": "Senior Software / Full Stack / Backend / Frontend / AI Engineer",
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
        "Senior Full Stack Engineer",
        "Senior Frontend Engineer",
        "AI Engineer",
        "Applied AI Engineer",
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
    "sde",
    "swe",
)
FULL_STACK_TERMS = ("full stack", "full-stack", "fullstack")
FOUNDING_TERMS = ("founding engineer", "founding software engineer")
FRONTEND_ONLY_TITLE_TERMS = (
    "frontend",
    "front end",
    "front-end",
    "ui engineer",
    "react engineer",
    "react developer",
    "ui developer",
    "web developer",
    "design engineer",
)
AI_ENGINEERING_TITLE_PATTERNS = (
    r"\b(?:ai|artificial intelligence|applied ai|generative ai|genai|llm|"
    r"machine learning|ml|mlops)\b.{0,40}\b(?:developer|engineer)\b",
    r"\b(?:developer|engineer)\b.{0,40}\b(?:ai|artificial intelligence|"
    r"applied ai|generative ai|genai|llm|machine learning|ml|mlops)\b",
)
EXCLUDED_ROLE_TERMS = (
    "designer",
    "product designer",
    "sales",
    "account executive",
    "customer success",
    "support",
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
)
ENGINEERING_DOMAIN_MODIFIER_TERMS = (
    "marketing",
    "growth",
    "operations",
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
    "director of software engineering",
    "director, engineering",
    "engineering director",
    "director, software engineering",
    "director software engineering",
    "head of engineering",
    "vp of engineering",
    "vice president of engineering",
    "developer advocate",
    "developer relations",
    "community engineer",
    "software engineer in test",
    "software development engineer in test",
    "sde manager",
    "manager, sde",
    "sdet",
)
ENGINEERING_TITLE_TERMS = (
    "engineer",
    "developer",
    "architect",
    "site reliability",
    "sre",
    "devops",
    "member of technical staff",
    "sde",
)
RESEARCH_ONLY_TERMS = (
    "research scientist",
    "ml researcher",
    "ai researcher",
    "machine learning researcher",
)
DATA_ANALYST_TERMS = ("data analyst", "business analyst", "analytics analyst")
JUNIOR_TITLE_PATTERNS = (
    r"\b(?:junior|jr\.?)\b",
    r"\bentry[- ]level\b",
    r"\b(?:new|recent)[- ]grad(?:uate)?\b",
    r"\bgraduate\s+(?:backend|full[- ]stack|platform|software|systems?)\s+"
    r"(?:developer|engineer)\b",
    r"\b(?:developer|engineer)\s*(?:[-–—,/:()]\s*)graduate\b",
)
QUALITY_ENGINEERING_TITLE_PATTERNS = (
    r"\b(?:software (?:development )?engineer|software developer)\b.{0,40}"
    r"\b(?:qa|quality(?: assurance)?|test(?: automation|ing)?)\b",
    r"\b(?:qa|quality(?: assurance)?|test(?: automation|ing)?)\b.{0,40}"
    r"\b(?:software engineer|software developer)\b",
)
ENGINEERING_LEADERSHIP_TITLE_PATTERNS = (
    r"\b(?:avp|evp|svp|vp|vice president)\b.{0,80}"
    r"\b(?:backend|engineering|engineer|infrastructure|platform|software|technology)\b",
    r"\b(?:backend|engineering|engineer|infrastructure|platform|software|technology)\b"
    r".{0,80}\b(?:avp|evp|svp|vp|vice president)\b",
    r"\b(?:senior|sr\.?)\s+(?:director|head|manager)\b.{0,60}"
    r"\b(?:backend|engineering|infrastructure|platform|software)\b",
    r"\b(?:director|head|manager)\b.{0,40}"
    r"\b(?:backend|infrastructure|platform|software)\s+engineering\b",
    r"\b(?:backend|infrastructure|platform|software)\s+engineering\b.{0,40}"
    r"\b(?:director|head|manager)\b",
    r"\b(?:senior|sr\.?)\s+(?:backend|infrastructure|platform|software)\s+"
    r"(?:director|head|manager)\b",
    r"\bhead\s+of\s+(?:backend|infrastructure|platform|software)"
    r"(?:\s+engineering)?\b",
    r"\b(?:engineer|engineering)\s+team\s+lead\b",
)
BUSINESS_DEVELOPMENT_TITLE_PATTERNS = (
    r"^\s*(?:(?:founding|global|lead|principal|regional|senior|sr\.?|strategic|"
    r"technical)\s+)*business development\b.{0,60}"
    r"\b(?:developer|engineer(?:ing)?|technical)\b",
    r"\bbusiness development\s+(?:associate|director|engineer|executive|lead|manager|"
    r"representative|specialist)\b",
    r"\b(?:director|head|lead|manager|vice president|vp)\s+(?:of\s+)?"
    r"business development\b",
    r"\bbusiness developer\b",
)
PHYSICAL_ENGINEERING_TITLE_PATTERNS = (
    r"\b(?:mechanical|stress|structural)\s+"
    r"(?:(?:analysis|design|systems?)\s+)?engineer(?:ing)?\b",
    r"\bengineer(?:ing)?\s*(?:[-–—,/:(]\s*|\s+)"
    r"(?:mechanical|stress|structural)\b",
    r"\bscada\b.{0,40}\bengineer(?:ing)?\b",
    r"\bengineer(?:ing)?\b.{0,24}\bscada\b",
    r"\b(?:project\s+)?commissioning\s+engineer(?:ing)?\b",
    r"\bdrilling\s+engineer(?:ing)?\b",
    r"\bplm\b.{0,32}\bengineer(?:ing)?\b",
    r"\bradar\s+systems?\s+engineer(?:ing)?\b",
    r"\bfpga\b.{0,48}\b(?:verification\s+)?engineer(?:ing)?\b",
    r"\bmission\s+architect\b",
    r"\bconverged\s+packet\s+optical\b",
)
DESIGN_PROCESS_TITLE_PATTERNS = (
    r"\bdesign\s*(?:&|and)\s*engineering\s+process\s+lead\b",
)
PHYSICAL_IT_IMPLEMENTATION_TITLE_PATTERNS = (
    r"\bsystems?\s+engineer\s+(?:i{1,3}|l{2}|[123])\b",
)
PHYSICAL_IT_CABLING_CONTEXT_PATTERNS = (
    r"\b(?:cable\s+management|cabling)\b",
)
PHYSICAL_IT_EQUIPMENT_CONTEXT_PATTERNS = (
    r"\bphysical\s+(?:setup|installation)\b.{0,120}"
    r"\b(?:access\s+points?|firewalls?|sans?|servers?|switches?)\b",
    r"\b(?:access\s+points?|firewalls?|sans?|servers?|switches?)\b.{0,120}"
    r"\bphysical\s+(?:setup|installation)\b",
)
NON_OPENING_CONTEXT_PATTERNS = (
    r"\bjoin\s+(?:our|the)\s+(?:contractor|freelance)\s+(?:pool|network)\b",
    r"\b(?:contractor|freelance)\s+network\b.{0,240}\bproject\s+invitations?\b",
    r"\bproject\s+invitations?\b.{0,160}\bwhen\s+they\s+arise\b",
    r"\bnot\s+(?:a|an|the)\s+(?:job|opening|position|role)\s+(?:that\s+)?we(?:'re|\s+are)?\s+currently\s+hiring\s+for\b",
    r"\bwe(?:'re|\s+are)\s+not\s+currently\s+hiring\s+for\s+(?:a|the|this)\s+(?:job|opening|position|role)\b",
    r"\baccepting\s+(?:applications?|resumes?)\s+for\s+(?:a\s+)?(?:potential\s+)?future\s+(?:job|opening|opportunit(?:y|ies)|position|role)\b",
)


@dataclass(frozen=True)
class RoleClassification:
    status: str
    reasons: list[str]


ROLE_FAMILY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("full_stack", (r"\bfull[ -]?stack\b",)),
    (
        "frontend",
        (
            r"\bfront[ -]?end\b",
            r"\bfrontend\b",
            r"\b(?:react|ui|web)\s+(?:developer|engineer)\b",
        ),
    ),
    ("ai_engineering", AI_ENGINEERING_TITLE_PATTERNS),
    ("backend", (r"\bback[ -]?end\b", r"\bbackend\b", r"\bapi\s+developer\b")),
    (
        "software_engineering",
        (
            r"\bsoftware\b",
            r"\b(?:sde|swe)\b",
        ),
    ),
    ("data_engineering", (r"\bdata\s+(?:developer|engineer)\b",)),
    (
        "platform_infrastructure",
        (
            r"\b(?:platform|infrastructure|cloud|devops)\b",
            r"\bsite reliability\b",
            r"\bsre\b",
        ),
    ),
)


def classify_role_family(title: str) -> str:
    """Return the user-facing engineering family represented by a job title."""
    normalized = title.casefold()
    for family, patterns in ROLE_FAMILY_PATTERNS:
        if _first_pattern_match(normalized, patterns):
            return family
    return "supporting_engineering"


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
REMOTE_ELIGIBILITY_ORDER = {
    "pakistan_explicit": 0,
    "global_explicit": 1,
    "regional_unconfirmed": 2,
    "remote_unclear": 3,
    "restricted_remote": 4,
    "onsite_explicit": 5,
    "no_remote_evidence": 6,
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
    evidence: list[str] = field(default_factory=list)


_GLOBAL_REMOTE_LOCATION_PATTERNS = (
    r"(?:worldwide|world wide)",
    r"home based (?:worldwide|world wide)",
    r"(?:worldwide|world wide) home based",
    r"remote anywhere(?: in (?:the )?world)?",
    r"anywhere(?: in (?:the )?world)? remote",
    r"global anywhere",
    r"anywhere global",
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
    r"100 percent remote",
    r"remote (?:eligible|first|optional|position|role|work)",
    r"(?:distributed|remote) first",
)
_GLOBAL_REMOTE_CLAIM_PATTERNS = (
    r"\b(?:this|the)\s+(?:job|role|position|opportunity)\s+(?:is|will\s+be|can\s+be|may\s+be)\s+(?:fully\s+)?remote(?:ly)?\s+(?:from\s+)?(?:anywhere|worldwide|world wide|globally)\b",
    r"\bwork\s+remotely\s+from\s+(?:anywhere|worldwide|world wide|any\s+country)\b",
    r"\bwork(?:ing)?\s+from\s+anywhere(?:\s+in\s+(?:the\s+)?world)?\b",
    r"\bwork(?:ing)?\s+from\s+any\s+(?:country|location)\b",
    r"\b(?:job|role|position|work)\s+(?:can|may)\s+be\s+performed\s+from\s+any\s+(?:country|location|place)\b",
    r"\bwork\s+(?:from\s+)?wherever\s+you\s+(?:are|live)\b",
    r"\b(?:based|located)\s+anywhere(?:\s+in\s+(?:the\s+)?world)?\b",
    r"\bopen\s+to\s+(?:candidates?|applicants?|employees?)\s+(?:based\s+)?(?:anywhere|worldwide|globally)\b",
    r"\bopen\s+to\s+(?:candidates?|applicants?|employees?)\s+(?:based\s+)?(?:in|from)\s+any\s+country\b",
    r"\b(?:hire|hiring|employ)\s+(?:people|employees?|candidates?|talent)?\s*(?:from|in)\s+(?:anywhere|any\s+country|the\s+world|worldwide|globally)\b",
    r"\b(?:eligible|supported)\s+(?:countries|locations|regions)\b.{0,80}\b(?:worldwide|anywhere|all\s+countries)\b",
    r"\blocation[- ](?:agnostic|independent)\b",
    r"\bno\s+(?:geographic|geographical|location|country)\s+restrictions\b",
)
_ROLE_REMOTE_CLAIM_PATTERNS = (
    r"\b(?:this|the)\s+(?:job|role|position|opportunity)\s+(?:is|will\s+be|can\s+be|may\s+be)\s+(?:a\s+)?(?:fully\s+|100%\s+)?remote\b",
    r"\bthis\s+is\s+(?:a\s+)?(?:fully\s+|100%\s+)?remote\s+(?:job|role|position|opportunity)\b",
    r"\b(?:fully\s+|100%\s+)?remote[- ](?:job|role|position|opportunity)\b",
    r"\bwork(?:ing)?\s+(?:fully\s+)?remotely\b",
    r"\b(?:can|may|will)\s+(?:work|be\s+performed)\s+remotely\b",
    r"\b(?:home[- ]based|telecommut(?:e|ing))\s+(?:job|role|position|opportunity)\b",
    r"\bfully\s+distributed\s+(?:job|role|position|opportunity|workforce)\b",
    r"\blocation[- ]flexible\s+(?:job|role|position|opportunity)\b",
)
_ONSITE_CLAIM_PATTERNS = (
    r"\b(?:on[- ]?site|in[- ]office|office[- ]based)\b",
    r"\b(?:this|the)\s+(?:job|role|position)\s+is\s+hybrid\b",
    r"\b(?:must|required\s+to)\s+(?:work|be)\s+(?:from|in|at)\s+(?:the\s+)?office\b",
)
_REMOTE_NEGATION_PATTERNS = (
    r"\b(?:this|the)\s+(?:job|role|position|opportunity)\s+is\s+not\s+remote\b",
    r"\bnot\s+a\s+remote\s+(?:job|role|position|opportunity)\b",
    r"\bremote(?:\s+work)?\s+(?:is\s+)?not\s+(?:available|offered|supported|permitted|an\s+option)\b",
)
_US_ELIGIBILITY_TERM_PATTERN = r"(?:u\.?\s*s\.?|united\s+states)"
_US_SPECIFIC_ELIGIBILITY_RESTRICTION_PATTERNS = (
    rf"\b{_US_ELIGIBILITY_TERM_PATTERN}\s+person\s+status\s+(?:is\s+)?required\b",
    rf"\b(?:this|the)\s+(?:position|role|job)\s+(?:will\s+)?"
    rf"requir(?:e|es|ed)\s+{_US_ELIGIBILITY_TERM_PATTERN}\s+citizenship\b",
    rf"\b{_US_ELIGIBILITY_TERM_PATTERN}\s+citizenship\s+(?:is\s+)?required\b",
    rf"\b{_US_ELIGIBILITY_TERM_PATTERN}\s+citizenship\s+and\s+(?:the\s+)?"
    rf"ability\s+to\s+(?:obtain|hold)(?:\s+and\s+maintain)?\s+(?:an?\s+)?"
    rf"{_US_ELIGIBILITY_TERM_PATTERN}\s+"
    rf"(?:(?:personnel|security|secret|top\s+secret|ts(?:\s*[/\-]\s*sci)?)\s+)*"
    rf"clearance\b",
    rf"\bmust\s+be\s+(?:an?\s+)?{_US_ELIGIBILITY_TERM_PATTERN}\s+citizen\b",
    rf"\b(?:applicants?|candidates?|employees?)\s+must\s+be\s+"
    rf"(?:legally\s+)?authorized\s+to\s+work\s+in\s+(?:the\s+)?"
    rf"{_US_ELIGIBILITY_TERM_PATTERN}\b",
    rf"\b(?:must|required\s+to)\s+(?:be\s+)?(?:legally\s+)?authorized\s+"
    rf"to\s+work\s+in\s+(?:the\s+)?{_US_ELIGIBILITY_TERM_PATTERN}\b",
    rf"\b(?:must\s+be\s+)?eligible\s+to\s+(?:obtain|hold)"
    rf"(?:\s+and\s+maintain)?\s+(?:an?\s+)?{_US_ELIGIBILITY_TERM_PATTERN}\s+"
    rf"(?:(?:personnel|security|secret|top\s+secret|ts(?:\s*[/\-]\s*sci)?)\s+)*"
    rf"clearance\b",
    rf"\b(?:this|the)\s+(?:position|role|job)\s+requires?\s+eligibility\s+to\s+"
    rf"(?:obtain|hold)(?:\s+and\s+maintain)?\s+(?:an?\s+)?"
    rf"{_US_ELIGIBILITY_TERM_PATTERN}\s+"
    rf"(?:(?:personnel|security|secret|top\s+secret|ts(?:\s*[/\-]\s*sci)?)\s+)*"
    rf"clearance\b",
    r"\b(?:ability|eligible|eligibility|required)\s+to\s+(?:obtain|hold)"
    r"(?:\s+and\s+maintain)?\s+(?:an?\s+)?public\s+trust(?:\s+clearance)?\b",
    rf"\bto\s+conform\s+to\s+{_US_ELIGIBILITY_TERM_PATTERN}\s+government\s+"
    rf"export\s+regulations?\b",
)
_OPTIONAL_ELIGIBILITY_BEFORE_PATTERN = re.compile(
    r"\b(?:optional|preferred|nice[- ]to[- ]have|a\s+plus|not\s+required)\b"
    r"[^.!?;\r\n]{0,100}$",
    flags=re.IGNORECASE,
)
_OPTIONAL_ELIGIBILITY_AFTER_PATTERN = re.compile(
    r"^[^.!?;\r\n]{0,50}\b(?:optional|preferred|nice[- ]to[- ]have|"
    r"a\s+plus|not\s+required)\b",
    flags=re.IGNORECASE,
)
_ELIGIBILITY_CLAUSE_BOUNDARY_PATTERN = re.compile(
    r"</?(?:br|div|h[1-6]|li|ol|p|ul)\b[^>]*>",
    flags=re.IGNORECASE,
)
_GLOBAL_SCOPE_LIMITATION_PATTERNS = (
    r"\bnot\s+(?:a\s+)?(?:remote\s+)?(?:worldwide|world wide|global(?:ly)?)(?:\s+remote)?\b",
    r"\bremote\s+(?:but\s+)?not\s+(?:worldwide|world wide|global(?:ly)?)\b",
    r"\b(?:worldwide|world wide|global(?:ly)?)\s+remote\s+(?:is\s+)?not\s+(?:available|offered|supported)\b",
    r"\b(?:work\s+from\s+)?anywhere\s+(?:in|within|across)\s+(?!the\s+world\b|worldwide\b|any\s+country\b)[a-z][a-z .'-]{0,60}",
    r"\b(?:anywhere|worldwide|world wide|global(?:ly)?)\b.{0,80}\b(?:except|excluding|other\s+than|but\s+not)\b",
    r"\b(?:supported|approved|eligible)\s+(?:countries|locations)\s+only\b",
    r"\b(?:countries|locations)\s+where\s+we\s+(?:have|operate|can\s+hire|can\s+employ)\b",
    r"\bwhere\s+we\s+have\s+(?:an?\s+)?(?:entity|payroll|office)\b",
    r"\b(?:located|based|work(?:ing)?)\s+anywhere\s+(?:that|where)\s+(?:our|the|an?)?\s*(?:eor|employer\s+of\s+record|payroll|entity)\b.{0,80}\b(?:supports?|allows?|operates?)\b",
)
_PAKISTAN_EXCLUSION_PATTERNS = (
    r"\b(?:except|excluding|excludes?|excluded|other\s+than|but\s+not)\b.{0,60}\bpakistan\b",
    r"\b(?:not\s+available|not\s+open)\s+(?:to|in|from)\b.{0,50}\bpakistan\b",
    r"\b(?:do\s+not|cannot|can't|unable\s+to)\s+(?:hire|employ|accept)\b.{0,60}\bpakistan\b",
    r"\bpakistan\b.{0,50}\b(?:not|isn't|is\s+not|are\s+not)\s+(?:eligible|supported|included|available|permitted)\b",
    r"\b(?:must\s+not|cannot)\s+(?:be\s+)?(?:based|located|reside|live)\s+in\s+pakistan\b",
)
_PAKISTAN_ELIGIBILITY_PATTERNS = (
    r"\b(?:remote|work\s+remotely|working\s+remotely)\s+(?:from|in)\s+pakistan\b",
    r"\bpakistan[- ,/]*(?:based\s+)?(?:remote|eligible|supported|accepted)\b",
    r"\b(?:candidates?|applicants?|employees?|contractors?)\s+(?:based|located|living|residing)\s+in\s+pakistan\b",
    r"\b(?:open|available)\s+to\s+(?:candidates?|applicants?|employees?)\s+(?:based\s+)?(?:in|from)\s+pakistan\b",
    r"\b(?:hire|hiring|employ)\s+(?:people|employees?|candidates?|talent)?\s*(?:in|from)\s+pakistan\b",
    r"\b(?:eligible|supported)\s+(?:countries|locations|regions)\b.{0,100}\bpakistan\b",
)
_REGIONAL_UNCONFIRMED_PATTERNS = (
    r"\bapac\b",
    r"\basia[- ]pacific\b",
    r"\bsouth\s+asia\b",
    r"\basia\b",
)
_RESTRICTED_REGION_PATTERNS = (
    r"\b(?:the\s+)?united\s+states\b",
    r"\bu\.?s\.?(?:a\.)?\b",
    r"\bcontiguous\s+(?:united\s+states|u\.?s\.?)\b",
    r"\bcanada\b",
    r"\bnorth\s+america\b",
    r"\blatin\s+america\b",
    r"\blatam\b",
    r"\beurope(?:an\s+union)?\b",
    r"\beu\b",
    r"\b(?:the\s+)?united\s+kingdom\b",
    r"\bu\.?k\.?\b",
    r"\baustralia\b",
    r"\bsingapore\b",
    r"\bindia\b",
    r"\bjapan\b",
    r"\bnew\s+zealand\b",
    r"\bphilippines\b",
    r"\bemea\b",
    r"\bamericas\b",
)
_TIMEZONE_CONSTRAINT_PATTERNS = (
    r"\b(?:utc|gmt)\s*[+-]\s*\d{1,2}(?::\d{2})?\b",
    r"\b(?:u\.?s\.?|european|japan|australia|apac)\s+(?:business\s+)?(?:hours|time\s+zones?)\b",
    r"\b(?:time\s+zones?|hours)\s+(?:between|within|overlapping)\b.{0,60}\b(?:utc|gmt)\b",
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


def _has_any_title_term(text: str, terms: tuple[str, ...]) -> bool:
    """Match exclusion terms as complete title tokens instead of substrings."""
    for term in terms:
        phrase = re.escape(term).replace(r"\ ", r"\s+")
        if re.search(rf"(?<![a-z0-9]){phrase}(?![a-z0-9])", text):
            return True
    return False


def classify_role_text(title: str, context: str = "") -> RoleClassification:
    title_text = title.lower()
    combined = f"{title} {context}".lower()
    is_full_stack = _has_any_signal(combined, FULL_STACK_TERMS)
    is_full_stack_title = _has_any_signal(title_text, FULL_STACK_TERMS)
    has_backend_signal = _has_any_signal(combined, BACKEND_ROLE_TERMS)
    has_backend_title_signal = _has_any_signal(title_text, BACKEND_ROLE_TERMS)
    has_software_signal = _has_any_signal(title_text, SOFTWARE_ROLE_TERMS)
    has_explicit_software_title = _has_signal(title_text, "software")
    has_backend_title_role = bool(
        re.search(
            r"(?:\bback[ -]?end\b.{0,32}\b(?:developer|engineer)\b|"
            r"\b(?:developer|engineer)\b.{0,32}\bback[ -]?end\b)",
            title_text,
        )
    )
    has_senior_signal = _has_any_signal(title_text, SENIOR_TERMS)
    is_founding = _has_any_signal(title_text, FOUNDING_TERMS)
    has_frontend_only_title = _has_any_signal(title_text, FRONTEND_ONLY_TITLE_TERMS)
    has_ai_engineering_title = bool(
        _first_pattern_match(title_text, AI_ENGINEERING_TITLE_PATTERNS)
    )

    if _first_pattern_match(title_text, JUNIOR_TITLE_PATTERNS):
        return RoleClassification("exclude", ["Junior or entry-level role is outside senior lane"])
    if _has_any_title_term(title_text, EXCLUDED_ROLE_TERMS):
        return RoleClassification("exclude", ["Non-engineering or junior/intern role"])
    if _first_pattern_match(title_text, ENGINEERING_LEADERSHIP_TITLE_PATTERNS):
        return RoleClassification("exclude", ["Engineering leadership role is outside IC SWE lane"])
    if _first_pattern_match(title_text, DESIGN_PROCESS_TITLE_PATTERNS):
        return RoleClassification(
            "exclude", ["Design-process role is outside the IC SWE lane"]
        )
    if _first_pattern_match(title_text, BUSINESS_DEVELOPMENT_TITLE_PATTERNS):
        return RoleClassification(
            "exclude", ["Business-development role is outside the IC SWE lane"]
        )
    if _has_any_title_term(title_text, ENGINEERING_DOMAIN_MODIFIER_TERMS) and not (
        has_software_signal or is_full_stack_title or has_backend_title_signal
    ):
        return RoleClassification(
            "exclude", ["Business-domain role lacks an explicit software/backend title"]
        )
    if _has_any_title_term(title_text, EXCLUDED_ENGINEERING_TITLE_TERMS):
        return RoleClassification("exclude", ["Engineering-adjacent role is outside the IC SWE lane"])
    if _first_pattern_match(title_text, QUALITY_ENGINEERING_TITLE_PATTERNS):
        return RoleClassification("exclude", ["QA/test role is outside the IC SWE lane"])
    if _first_pattern_match(title_text, PHYSICAL_ENGINEERING_TITLE_PATTERNS) and not (
        has_explicit_software_title or is_full_stack_title or has_backend_title_role
    ):
        return RoleClassification(
            "exclude", ["Physical or industrial engineering role is outside software lane"]
        )
    if (
        _first_pattern_match(title_text, PHYSICAL_IT_IMPLEMENTATION_TITLE_PATTERNS)
        and _first_pattern_match(combined, PHYSICAL_IT_CABLING_CONTEXT_PATTERNS)
        and _first_pattern_match(combined, PHYSICAL_IT_EQUIPMENT_CONTEXT_PATTERNS)
        and not (has_explicit_software_title or is_full_stack_title or has_backend_title_role)
    ):
        return RoleClassification(
            "exclude", ["Physical IT implementation role is outside software lane"]
        )
    if _first_pattern_match(combined, NON_OPENING_CONTEXT_PATTERNS):
        return RoleClassification("exclude", ["Listing is not a current opening"])
    if _has_any_signal(title_text, DATA_ANALYST_TERMS):
        return RoleClassification("exclude", ["Data analyst role is outside the engineering lane"])
    if _has_any_signal(title_text, RESEARCH_ONLY_TERMS) and not has_backend_signal:
        return RoleClassification(
            "exclude", ["Research-only ML role lacks backend/platform signal"]
        )
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
    if has_senior_signal and has_software_signal:
        return RoleClassification(
            "strong", ["Senior software engineering role matches target lane"]
        )
    if is_full_stack:
        return RoleClassification(
            "possible",
            [
                "Full-stack role has backend/API/data/infra signals"
                if has_backend_signal
                else "Full-stack engineering role matches expanded target lane"
            ],
        )
    if has_frontend_only_title:
        return RoleClassification(
            "possible", ["Frontend engineering role matches expanded target lane"]
        )
    if has_ai_engineering_title:
        return RoleClassification(
            "possible", ["Production AI/ML engineering role matches expanded target lane"]
        )
    if is_founding and has_backend_signal:
        return RoleClassification("possible", ["Founding role has backend-heavy signals"])
    if is_founding or is_full_stack or has_software_signal:
        return RoleClassification("weak", ["Engineering role exists, but target-lane fit is unclear"])
    return RoleClassification("weak", ["No clear target software engineering role signal"])


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


def _normalise_job_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#]+", " ", title.casefold())).strip()


_REQUISITION_ID_KEYS = (
    "requisition_id",
    "requisitionId",
    "requisitionID",
    "requisition_number",
    "requisitionNumber",
    "req_id",
    "reqId",
    "job_code",
    "jobCode",
)
_TRACKING_QUERY_KEYS = frozenset({"gh_src", "ref", "referrer", "source"})


def _normalise_job_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.casefold().rstrip(".").removeprefix("www.")
    if port is not None and port not in {80, 443}:
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in _TRACKING_QUERY_KEYS
        ),
        doseq=True,
    )
    return urlunsplit(("https", host, path, query, ""))


def _job_source_identity(job: dict[str, Any], fallback_index: int) -> str:
    company_source_id = job.get("company_source_id")
    if company_source_id is not None and str(company_source_id).strip():
        return f"company_source:{company_source_id}"
    provider = str(job.get("provider") or "").strip().casefold()
    source_external_id = str(job.get("source_external_id") or "").strip().casefold()
    if provider and source_external_id:
        return f"provider_source:{provider}:{source_external_id}"
    source_url = _normalise_job_url(job.get("source_url"))
    if provider and source_url:
        return f"provider_url:{provider}:{source_url}"
    if provider:
        return f"provider:{provider}"
    if source_url:
        return f"source_url:{source_url}"
    source_kind = str(job.get("source_kind") or "").strip().casefold()
    if source_kind:
        return f"source_kind:{source_kind}"
    return f"unknown_source:{fallback_index}"


def _job_source_external_identity(job: dict[str, Any], fallback_index: int) -> str:
    source = _job_source_identity(job, fallback_index)
    external_job_id = str(job.get("external_job_id") or "").strip()
    if external_job_id:
        return f"{source}:external_job:{external_job_id}"
    job_key = str(job.get("job_key") or "").strip()
    if job_key:
        return f"{source}:job_key:{job_key}"
    source_record_id = str(job.get("source_record_id") or "").strip()
    if source_record_id:
        return f"{source}:source_record:{source_record_id}"
    return f"{source}:row:{fallback_index}"


def _normalise_requisition_id(value: Any) -> str | None:
    raw = str(value or "").strip().casefold()
    if not raw:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "", raw)
    return normalized or None


def _job_requisition_id(job: dict[str, Any]) -> str | None:
    for key in _REQUISITION_ID_KEYS:
        if normalized := _normalise_requisition_id(job.get(key)):
            return normalized
    evidence = job.get("structured_evidence")
    if isinstance(evidence, dict):
        for key in _REQUISITION_ID_KEYS:
            if normalized := _normalise_requisition_id(evidence.get(key)):
                return normalized
    return None


def _job_cross_source_anchors(job: dict[str, Any]) -> dict[str, set[str]]:
    urls: set[str] = set()
    for value in (job.get("posting_url"), job.get("apply_url")):
        if normalized := _normalise_job_url(value):
            urls.add(f"normalized_url:{normalized}")
    evidence = job.get("structured_evidence")
    application = evidence.get("application") if isinstance(evidence, dict) else None
    if isinstance(application, dict):
        for key in ("posting_url", "apply_url"):
            if normalized := _normalise_job_url(application.get(key)):
                urls.add(f"normalized_url:{normalized}")

    title = _normalise_job_title(str(job.get("title") or ""))
    requisitions: set[str] = set()
    if title and (requisition_id := _job_requisition_id(job)):
        requisitions.add(f"requisition_id:{requisition_id}:title:{title}")
    content: set[str] = set()
    content_hash = str(job.get("content_hash") or "").strip().casefold()
    if content_hash and title:
        content.add(f"content_hash:{content_hash}:title:{title}")
    return {"url": urls, "requisition": requisitions, "content": content}


def _cluster_jobs(
    jobs: list[dict[str, Any]],
) -> list[tuple[list[dict[str, Any]], list[str]]]:
    """Group only source variants connected by conservative, auditable identity anchors."""
    records = [
        {
            "job": job,
            "source": _job_source_identity(job, index),
            "source_external": _job_source_external_identity(job, index),
            "anchors": _job_cross_source_anchors(job),
        }
        for index, job in enumerate(jobs)
    ]
    groups = [
        {"members": {index}, "anchors": set()} for index in range(len(records))
    ]
    direct_matches: dict[tuple[int, int], tuple[int, tuple[str, ...]]] = {}
    edges: list[tuple[int, int, int, tuple[str, ...]]] = []
    for left_index, left in enumerate(records):
        for right_index in range(left_index + 1, len(records)):
            right = records[right_index]
            reasons: set[str] = set()
            priority = 99
            if left["source_external"] == right["source_external"]:
                priority = 0
                reasons.add(f"source_external_id:{left['source_external']}")
            elif left["source"] != right["source"]:
                for anchor_kind, anchor_priority in (
                    ("url", 1),
                    ("requisition", 2),
                    ("content", 3),
                ):
                    shared = left["anchors"][anchor_kind] & right["anchors"][anchor_kind]
                    if shared:
                        priority = min(priority, anchor_priority)
                        reasons.update(shared)
            if reasons:
                match = (priority, tuple(sorted(reasons)))
                direct_matches[(left_index, right_index)] = match
                edges.append((priority, left_index, right_index, match[1]))

    def group_for(index: int) -> dict[str, Any]:
        return next(group for group in groups if index in group["members"])

    def complete_linkage_reasons(
        left: dict[str, Any], right: dict[str, Any]
    ) -> set[str] | None:
        reasons: set[str] = set()
        for left_member in left["members"]:
            for right_member in right["members"]:
                pair = tuple(sorted((left_member, right_member)))
                match = direct_matches.get(pair)
                if match is None:
                    return None
                reasons.update(match[1])
        return reasons

    for _, left_index, right_index, reasons in sorted(edges):
        left = group_for(left_index)
        right = group_for(right_index)
        if left is right:
            left["anchors"].update(reasons)
            continue
        merge_reasons = complete_linkage_reasons(left, right)
        if merge_reasons is None:
            continue
        left["members"].update(right["members"])
        left["anchors"].update(right["anchors"])
        left["anchors"].update(merge_reasons)
        groups.remove(right)

    groups.sort(key=lambda group: min(group["members"]))
    return [
        (
            [records[index]["job"] for index in sorted(group["members"])],
            sorted(group["anchors"]),
        )
        for group in groups
    ]


def _cluster_matching_job_provenance(
    classifications: list[tuple[str, RoleClassification, dict[str, Any] | None]],
) -> list[dict[str, Any]]:
    matching_jobs: list[dict[str, Any]] = []
    for _, classification, job in classifications:
        if job is None or classification.status not in {"strong", "possible"}:
            continue
        matching_jobs.append(job)

    clusters: list[dict[str, Any]] = []
    for jobs, identity_anchors in _cluster_jobs(matching_jobs):
        variants = [_job_provenance(job) for job in jobs]
        representative = min(
            variants,
            key=lambda item: (
                0 if item.get("lifecycle_managed") is True else 1,
                REMOTE_ELIGIBILITY_ORDER.get(str(item["remote_eligibility"]), 99),
                str(item.get("provider") or ""),
                str(item.get("external_job_id") or ""),
            ),
        )
        status_distribution: dict[str, int] = {}
        for variant in variants:
            status = str(variant["remote_eligibility"])
            status_distribution[status] = status_distribution.get(status, 0) + 1

        cluster = dict(representative)
        cluster["posting_variant_count"] = len(variants)
        cluster["identity_anchors"] = identity_anchors
        cluster["remote_eligibility_distribution"] = status_distribution
        cluster["posting_variants"] = variants
        clusters.append(cluster)
    return clusters


def role_focus_record(
    company: Company,
    *,
    jobs: list[dict[str, Any]] | None = None,
    verified_roles: list[str] | None = None,
) -> dict[str, Any]:
    role_inputs: list[tuple[str, str, dict[str, Any] | None]] = []
    inventory_jobs = [dict(job) for job in jobs or []]
    for job in inventory_jobs:
        title = str(job.get("title") or "").strip()
        if title:
            role_inputs.append((title, _job_context(job), job))
    for role in verified_roles or []:
        title = str(role).strip()
        if title:
            role_inputs.append((title, title, None))

    classifications = [
        (title, classify_role_text(title, context), inventory_job)
        for title, context, inventory_job in role_inputs
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
            reasons = ["No public target software engineering role found yet"]

    if status == "strong":
        target_role_lane = "Senior Software / Backend / Full Stack / Frontend / AI"
        application_angle = (
            "Apply directly as a senior engineer and lead with the proof points that match the "
            "role family: backend systems, full-stack delivery, React, data, or production AI."
        )
    elif status == "possible":
        target_role_lane = "Software / Full Stack / Frontend / AI Engineering"
        application_angle = (
            "Apply when the stack and level fit, emphasizing shipped systems and end-to-end "
            "engineering judgment rather than relying on the title alone."
        )
    elif status == "weak":
        target_role_lane = "Unclear software engineering fit"
        application_angle = (
            "Keep as research-only until the posting shows a concrete software, frontend, "
            "full-stack, backend, or production-AI engineering fit."
        )
    else:
        target_role_lane = "Outside target engineering focus"
        application_angle = "Do not prioritize for the software engineering shortlist."

    matching_provenance_all = _cluster_matching_job_provenance(classifications)
    remote_counts: dict[str, int] = {}
    for job in matching_provenance_all:
        remote_status = str(job["remote_eligibility"])
        remote_counts[remote_status] = remote_counts.get(remote_status, 0) + 1
    best_remote = min(
        remote_counts,
        key=lambda status: REMOTE_ELIGIBILITY_ORDER.get(status, 99),
        default="no_remote_evidence",
    )
    inventory_classifications = [
        (title, classification)
        for title, classification, job in classifications
        if job is not None
    ]
    inventory_role_status = (
        _best_status(inventory_classifications) if inventory_classifications else "none"
    )
    active_cluster_count = len(_cluster_jobs(inventory_jobs))
    raw_active_count = len(inventory_jobs)
    raw_matching_count = sum(
        1
        for _, classification, job in classifications
        if job is not None and classification.status in {"strong", "possible"}
    )
    lifecycle_managed_jobs = [
        job for job in inventory_jobs if job.get("lifecycle_managed") is True
    ]
    lifecycle_managed_classifications = [
        (title, classification, job)
        for title, classification, job in classifications
        if job is not None and job.get("lifecycle_managed") is True
    ]
    lifecycle_managed_matching_jobs = _cluster_matching_job_provenance(
        lifecycle_managed_classifications
    )
    lifecycle_managed_active_cluster_count = len(
        _cluster_jobs(lifecycle_managed_jobs)
    )
    lifecycle_managed_raw_matching_count = sum(
        1
        for _, classification, _ in lifecycle_managed_classifications
        if classification.status in {"strong", "possible"}
    )
    lifecycle_managed_role_status = (
        _best_status(
            [
                (title, classification)
                for title, classification, _ in lifecycle_managed_classifications
            ]
        )
        if lifecycle_managed_classifications
        else "none"
    )
    lifecycle_managed_remote_counts: dict[str, int] = {}
    for job in lifecycle_managed_matching_jobs:
        remote_status = str(job["remote_eligibility"])
        lifecycle_managed_remote_counts[remote_status] = (
            lifecycle_managed_remote_counts.get(remote_status, 0) + 1
        )
    lifecycle_managed_best_remote = min(
        lifecycle_managed_remote_counts,
        key=lambda remote_status: REMOTE_ELIGIBILITY_ORDER.get(remote_status, 99),
        default="no_remote_evidence",
    )
    result = {
        "target_role_lane": target_role_lane,
        "matching_job_titles": _dedupe_preserve_order(matching_titles)[:MAX_MATCHING_JOB_DETAILS],
        "active_job_count": active_cluster_count,
        "raw_active_job_count": raw_active_count,
        "duplicate_posting_count": raw_active_count - active_cluster_count,
        "matching_job_count": len(matching_provenance_all),
        "raw_matching_job_count": raw_matching_count,
        "duplicate_matching_job_count": raw_matching_count - len(matching_provenance_all),
        "job_role_match_status": inventory_role_status,
        "matching_jobs": matching_provenance_all,
        "matching_job_provenance": matching_provenance_all,
        "best_remote_eligibility": best_remote,
        "pakistan_explicit_matching_job_count": remote_counts.get(
            "pakistan_explicit", 0
        ),
        "global_explicit_matching_job_count": remote_counts.get("global_explicit", 0),
        "regional_unconfirmed_matching_job_count": remote_counts.get(
            "regional_unconfirmed", 0
        ),
        "remote_unclear_matching_job_count": remote_counts.get("remote_unclear", 0),
        # Keep the two historical field names as aliases for existing CSV consumers.
        "globally_remote_matching_job_count": remote_counts.get("global_explicit", 0),
        "pakistan_compatible_matching_job_count": remote_counts.get(
            "pakistan_explicit", 0
        ),
        "remote_matching_job_count": sum(
            remote_counts.get(status, 0)
            for status in (
                "pakistan_explicit",
                "global_explicit",
                "regional_unconfirmed",
                "remote_unclear",
            )
        ),
        "role_match_status": status,
        "role_match_reasons": _dedupe_preserve_order(reasons)[:5],
        "application_angle": application_angle,
        "proof_points_to_emphasize": proof_points_for_role_status(status),
    }
    result.update(
        {
            "managed_active_job_count": lifecycle_managed_active_cluster_count,
            "managed_raw_active_job_count": len(lifecycle_managed_jobs),
            "managed_duplicate_posting_count": (
                len(lifecycle_managed_jobs) - lifecycle_managed_active_cluster_count
            ),
            "managed_matching_job_count": len(lifecycle_managed_matching_jobs),
            "managed_raw_matching_job_count": lifecycle_managed_raw_matching_count,
            "managed_duplicate_matching_job_count": (
                lifecycle_managed_raw_matching_count
                - len(lifecycle_managed_matching_jobs)
            ),
            "managed_role_match_status": lifecycle_managed_role_status,
            "managed_matching_jobs": lifecycle_managed_matching_jobs,
            "managed_best_remote_eligibility": lifecycle_managed_best_remote,
        }
    )
    return result


def _job_provenance(job: dict[str, Any]) -> dict[str, Any]:
    def iso(value: Any) -> Any:
        return value.isoformat() if hasattr(value, "isoformat") else value

    remote = classify_remote_eligibility(job)
    return {
        "job_key": job.get("job_key"),
        "source_kind": job.get("source_kind"),
        "source_record_id": str(job.get("source_record_id") or ""),
        "title": job.get("title"),
        "provider": job.get("provider"),
        "external_job_id": str(job.get("external_job_id") or ""),
        "company_source_id": job.get("company_source_id"),
        "source_external_id": job.get("source_external_id"),
        "source_url": job.get("source_url"),
        "posting_url": job.get("posting_url"),
        "location": job.get("location"),
        "department": job.get("department"),
        "remote_eligibility": remote.status,
        "remote_reasons": remote.reasons,
        "remote_evidence": remote.evidence,
        "structured_evidence": job.get("structured_evidence"),
        "status": job.get("status"),
        "lifecycle_managed": job.get("lifecycle_managed"),
        "status_confidence": job.get("status_confidence"),
        "source_published_at": iso(job.get("source_published_at")),
        "source_updated_at": iso(job.get("source_updated_at")),
    }


@dataclass(frozen=True)
class _StructuredRemoteEvidence:
    remote: bool = False
    onsite: bool = False
    workplace_type: str = ""
    countries: tuple[str, ...] = ()
    primary_location_label: str = ""
    location_labels: tuple[str, ...] = ()
    eligibility_text: str = ""


def _stringify_evidence_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return " ".join(
            part
            for key, item in value.items()
            if (part := f"{key} {_stringify_evidence_value(item)}".strip())
        )
    if isinstance(value, (list, tuple, set)):
        return " ".join(
            part for item in value if (part := _stringify_evidence_value(item))
        )
    return str(value).strip()


def _structured_remote_evidence(job: dict[str, Any]) -> _StructuredRemoteEvidence:
    raw = job.get("structured_evidence")
    if not isinstance(raw, dict):
        return _StructuredRemoteEvidence()

    workplace = raw.get("workplace")
    workplace = workplace if isinstance(workplace, dict) else {}
    workplace_type = _normalise_remote_location(str(workplace.get("type") or ""))
    is_remote = workplace.get("is_remote")
    remote = workplace_type == "remote" or is_remote is True
    onsite = workplace_type in {"hybrid", "on site", "onsite"} or is_remote is False

    countries: list[str] = []
    raw_countries = raw.get("countries")
    if isinstance(raw_countries, list):
        countries.extend(str(value).strip() for value in raw_countries if value)

    primary_label = ""
    locations: list[dict[str, Any]] = []
    primary = raw.get("primary_location")
    if isinstance(primary, dict):
        locations.append(primary)
        primary_label = str(primary.get("label") or "").strip()
    secondary = raw.get("secondary_locations")
    if isinstance(secondary, list):
        structured_secondary = [
            value for value in secondary if isinstance(value, dict)
        ]
        locations.extend(structured_secondary)

    labels: list[str] = []
    for structured_location in locations:
        country = str(structured_location.get("country") or "").strip()
        label = str(structured_location.get("label") or "").strip()
        if country:
            countries.append(country)
        if label:
            labels.append(label)

    eligibility_parts: list[str] = []
    signals = raw.get("eligibility_signals")
    if isinstance(signals, list):
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            name = str(signal.get("name") or "").strip()
            value = _stringify_evidence_value(signal.get("value"))
            if name or value:
                eligibility_parts.append(f"{name} {value}".strip())

    return _StructuredRemoteEvidence(
        remote=remote,
        onsite=onsite,
        workplace_type=workplace_type,
        countries=tuple(_dedupe_preserve_order(countries)),
        primary_location_label=primary_label,
        location_labels=tuple(_dedupe_preserve_order(labels)),
        eligibility_text=" ".join(eligibility_parts),
    )


def _remote_result(
    status: str,
    reason: str,
    *evidence: str,
) -> RemoteEligibility:
    return RemoteEligibility(
        status,
        [reason],
        _dedupe_preserve_order([item for item in evidence if item]),
    )


def _evidence(label: str, value: str, *, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    if not compact:
        return ""
    if len(compact) > limit:
        compact = f"{compact[: limit - 1].rstrip()}…"
    return f"{label}: {compact}"


def classify_remote_eligibility(job: dict[str, Any]) -> RemoteEligibility:
    title_raw = str(job.get("title") or "").strip()
    location_raw = str(job.get("location") or "").strip()
    description_raw = str(job.get("description_text") or "").strip()
    location = location_raw.casefold()
    description = description_raw.casefold()
    structured = _structured_remote_evidence(job)
    structured_labels = " | ".join(structured.location_labels).casefold()
    countries = " | ".join(structured.countries).casefold()
    eligibility_text = structured.eligibility_text.casefold()
    descriptive_evidence = " ".join(
        part for part in (description, eligibility_text) if part
    )
    location_scope = " | ".join(
        _dedupe_preserve_order(
            [part for part in (location, structured_labels) if part]
        )
    )
    location_is_remote = bool(re.search(r"\bremote\b", location_scope))
    # Greenhouse repeats the provider's primary label in both ``location`` and
    # ``structured_evidence.primary_location``. Treat that one label as the authoritative
    # location scope. Office membership and other posting locations are useful provenance, but
    # they do not narrow an otherwise unqualified worldwide primary label.
    primary_location_scope = structured.primary_location_label or location_raw
    primary_location_for_global_check = primary_location_scope
    if (
        structured.remote
        and primary_location_scope
        and not re.search(r"\bremote\b", primary_location_scope, flags=re.IGNORECASE)
    ):
        primary_location_for_global_check = f"remote {primary_location_scope}"
    global_primary_location = (
        primary_location_scope
        if _is_unambiguously_global_remote_location(primary_location_for_global_check)
        else ""
    )
    global_location_signal = _first_pattern_match(
        primary_location_scope, _GLOBAL_REMOTE_LOCATION_PATTERNS
    )
    title_remote_signal = _title_work_arrangement_remote_match(title_raw) is not None
    restricted_title_remote_scope = _title_remote_region_match(
        title_raw, _RESTRICTED_REGION_PATTERNS
    )
    regional_title_remote_scope = _title_remote_region_match(
        title_raw, _REGIONAL_UNCONFIRMED_PATTERNS
    )
    global_title_remote_scope = _title_global_remote_scope_match(title_raw)

    global_match = _first_pattern_match(descriptive_evidence, _GLOBAL_REMOTE_CLAIM_PATTERNS)
    role_remote_match = _first_pattern_match(
        descriptive_evidence, _ROLE_REMOTE_CLAIM_PATTERNS
    )
    remote_signal = bool(
        location_is_remote
        or structured.remote
        or title_remote_signal
        or global_location_signal
        or global_match
        or role_remote_match
    )

    us_specific_restriction = _first_required_us_eligibility_match(
        descriptive_evidence
    )
    if us_specific_restriction and remote_signal:
        return _remote_result(
            "restricted_remote",
            "Posting explicitly requires U.S.-specific status or government eligibility",
            _evidence("eligibility restriction", us_specific_restriction),
        )

    pakistan_exclusion = _first_pattern_match(
        " ".join((location_scope, countries, descriptive_evidence)),
        _PAKISTAN_EXCLUSION_PATTERNS,
    )
    if pakistan_exclusion and remote_signal:
        return _remote_result(
            "restricted_remote",
            "Pakistan is explicitly excluded from eligibility",
            _evidence("restriction", pakistan_exclusion),
        )

    global_limitation = _first_pattern_match(
        " ".join((location_scope, descriptive_evidence)),
        _GLOBAL_SCOPE_LIMITATION_PATTERNS,
    )
    if global_limitation and remote_signal:
        return _remote_result(
            "restricted_remote",
            "Remote eligibility is explicitly narrower than worldwide",
            _evidence("restriction", global_limitation),
        )

    restricted_title_scope = _title_region_only_match(
        title_raw, _RESTRICTED_REGION_PATTERNS
    ) or restricted_title_remote_scope
    if restricted_title_scope and remote_signal:
        return _remote_result(
            "restricted_remote",
            "Job title explicitly limits the remote role to a region outside Pakistan",
            _evidence("title restriction", restricted_title_scope),
        )

    regional_title_scope = _title_region_only_match(
        title_raw, _REGIONAL_UNCONFIRMED_PATTERNS
    ) or regional_title_remote_scope
    if regional_title_scope and remote_signal:
        return _remote_result(
            "regional_unconfirmed",
            "Job title explicitly limits the remote role to a broad region",
            _evidence("title restriction", regional_title_scope),
        )

    legal_work_countries = _legal_work_country_list(description_raw)
    if legal_work_countries and remote_signal and not legal_work_countries[1]:
        return _remote_result(
            "restricted_remote",
            "Posting limits legal work authorization to enumerated countries outside Pakistan",
            _evidence("legal work countries", legal_work_countries[0]),
        )

    remote_negation = _first_pattern_match(description, _REMOTE_NEGATION_PATTERNS)
    location_onsite_match = _first_pattern_match(location, _ONSITE_CLAIM_PATTERNS)
    description_onsite_match = _first_pattern_match(
        description, _ONSITE_CLAIM_PATTERNS
    )
    onsite_match = description_onsite_match or (
        location_onsite_match if not global_primary_location else None
    )
    if structured.onsite or remote_negation or onsite_match:
        workplace_evidence = (
            _evidence("structured workplace", structured.workplace_type)
            if structured.onsite
            else ""
        )
        return _remote_result(
            "onsite_explicit",
            "Posting explicitly requires onsite or hybrid work",
            workplace_evidence,
            _evidence("onsite signal", remote_negation or onsite_match or ""),
        )

    country_values = [value.casefold() for value in structured.countries]
    country_evidence = _evidence(
        "structured posting countries", ", ".join(structured.countries)
    )
    location_only_country_evidence = ""
    if country_values:
        if remote_signal and any(
            _matches_any_pattern(value, _RESTRICTED_REGION_PATTERNS)
            for value in country_values
        ):
            return _remote_result(
                "restricted_remote",
                "Structured posting location restricts the role outside Pakistan",
                country_evidence,
            )
        if remote_signal and any(
            _matches_any_pattern(value, _REGIONAL_UNCONFIRMED_PATTERNS)
            for value in country_values
        ):
            return _remote_result(
                "regional_unconfirmed",
                "A broad posting region is named, but applicant eligibility is unconfirmed",
                country_evidence,
            )
        location_only_country_evidence = country_evidence
        has_unconfirmed_pakistan_or_global_location = any(
            re.search(r"\bpakistan\b", value) or _is_global_scope_value(value)
            for value in country_values
        )
        if remote_signal and not has_unconfirmed_pakistan_or_global_location:
            return _remote_result(
                "restricted_remote",
                "Structured posting location restricts the remote role outside Pakistan",
                country_evidence,
            )

    scope_with_remote = location_scope
    if (
        (structured.remote or global_location_signal)
        and scope_with_remote
        and not location_is_remote
    ):
        scope_with_remote = f"remote {scope_with_remote}"

    if (
        not global_primary_location
        and _structured_remote_location_is_restricted(scope_with_remote)
    ):
        return _remote_result(
            "restricted_remote",
            "Structured remote location is explicitly geographically restricted",
            _evidence("location", location_raw or " | ".join(structured.location_labels)),
        )

    restricted_region_match = _region_eligibility_match(
        descriptive_evidence, _RESTRICTED_REGION_PATTERNS
    )
    generic_restriction_match = _generic_location_restriction_match(descriptive_evidence)
    if remote_signal and (restricted_region_match or generic_restriction_match):
        return _remote_result(
            "restricted_remote",
            "Remote eligibility is explicitly restricted outside Pakistan",
            _evidence(
                "restriction",
                restricted_region_match or generic_restriction_match or "",
            ),
        )

    regional_description_match = _region_eligibility_match(
        descriptive_evidence, _REGIONAL_UNCONFIRMED_PATTERNS
    )
    timezone_match = _first_pattern_match(
        descriptive_evidence, _TIMEZONE_CONSTRAINT_PATTERNS
    )
    if global_primary_location and (regional_description_match or timezone_match):
        return _remote_result(
            "regional_unconfirmed",
            "Regional or timezone eligibility narrows an otherwise global location label",
            _evidence(
                "regional signal", regional_description_match or timezone_match or ""
            ),
        )

    if (
        remote_signal
        and location_scope
        and not location_is_remote
        and not structured.remote
        and not global_primary_location
    ):
        return _remote_result(
            "restricted_remote",
            "Posting pairs role-specific remote language with a specific location",
            _evidence("location", location_raw),
            _evidence("remote signal", role_remote_match or global_match or ""),
        )

    pakistan_match = _first_pattern_match(
        descriptive_evidence, _PAKISTAN_ELIGIBILITY_PATTERNS
    )
    if remote_signal and (
        re.search(r"\bpakistan\b", location_scope)
        or pakistan_match
        or (legal_work_countries and legal_work_countries[1])
    ):
        return _remote_result(
            "pakistan_explicit",
            "Remote eligibility explicitly names Pakistan",
            _evidence("location", location_raw) if "pakistan" in location else "",
            _evidence("eligibility", pakistan_match or ""),
            _evidence("legal work countries", legal_work_countries[0])
            if legal_work_countries and legal_work_countries[1]
            else "",
        )

    if global_title_remote_scope and remote_signal:
        return _remote_result(
            "global_explicit",
            "Job title explicitly scopes the role as worldwide remote",
            _evidence("title global scope", global_title_remote_scope),
        )

    if global_primary_location or _is_unambiguously_global_remote_location(
        scope_with_remote
    ):
        return _remote_result(
            "global_explicit",
            "Structured location explicitly allows worldwide remote work",
            _evidence("location", global_primary_location or location_raw),
        )

    regional_location_match = _first_pattern_match(
        scope_with_remote, _REGIONAL_UNCONFIRMED_PATTERNS
    )
    if remote_signal and (regional_location_match or regional_description_match or timezone_match):
        return _remote_result(
            "regional_unconfirmed",
            "Regional or timezone eligibility is present, but Pakistan is not explicit",
            _evidence(
                "regional signal",
                regional_location_match or regional_description_match or timezone_match or "",
            ),
        )

    if global_match:
        return _remote_result(
            "global_explicit",
            "Posting explicitly allows worldwide remote work",
            _evidence("global signal", global_match),
        )

    if remote_signal:
        return _remote_result(
            "remote_unclear",
            "Role is remote, but eligible countries are not explicit",
            _evidence("location", location_raw) if location_is_remote else "",
            _evidence("structured workplace", structured.workplace_type)
            if structured.remote
            else "",
            location_only_country_evidence,
            _evidence("remote signal", role_remote_match or ""),
        )

    return RemoteEligibility(
        "no_remote_evidence",
        ["No role-specific remote or onsite evidence was detected"],
        [],
    )


def _normalise_remote_location(location: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", location.lower())).strip()


def _first_pattern_match(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(0)).strip()
    return None


def _first_required_us_eligibility_match(text: str) -> str | None:
    for pattern in _US_SPECIFIC_ELIGIBILITY_RESTRICTION_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            before_raw = text[max(0, match.start() - 140) : match.start()]
            before_boundaries = list(
                _ELIGIBILITY_CLAUSE_BOUNDARY_PATTERN.finditer(before_raw)
            )
            if before_boundaries:
                before_raw = before_raw[before_boundaries[-1].end() :]
            after_raw = text[match.end() : match.end() + 100]
            after_boundary = _ELIGIBILITY_CLAUSE_BOUNDARY_PATTERN.search(after_raw)
            if after_boundary:
                after_raw = after_raw[: after_boundary.start()]
            before = re.sub(r"<[^>]+>", " ", before_raw)
            after = re.sub(r"<[^>]+>", " ", after_raw)
            if _OPTIONAL_ELIGIBILITY_BEFORE_PATTERN.search(before):
                continue
            if _OPTIONAL_ELIGIBILITY_AFTER_PATTERN.search(after):
                continue
            return re.sub(r"\s+", " ", match.group(0)).strip()
    return None


def _matches_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return _first_pattern_match(text, patterns) is not None


def _title_region_only_match(title: str, region_patterns: tuple[str, ...]) -> str | None:
    for region in region_patterns:
        match = _first_pattern_match(
            title,
            (
                rf"(?:{region})\s*(?:[-:/]\s*)?only\b",
                rf"\bonly\s+(?:(?:in|within|for)\s+)?(?:the\s+)?(?:{region})",
                rf"(?:{region})[- ]based\b",
                rf"\bbased\s+(?:in|within)\s+(?:the\s+)?(?:{region})",
            ),
        )
        if match:
            return match
    return None


def _title_remote_region_match(
    title: str, region_patterns: tuple[str, ...]
) -> str | None:
    if _title_work_arrangement_remote_match(title) is None:
        return None
    for region in region_patterns:
        match = _first_pattern_match(
            title,
            (
                rf"(?:{region})[-–—,/|:()\[\]\s]*remote\b",
                rf"\bremote[-–—,/|:()\[\]\s]*(?:the\s+)?(?:{region})"
                rf"(?:\s*[)\]])?",
            ),
        )
        if match:
            return match
    return None


def _title_global_remote_scope_match(title: str) -> str | None:
    return _first_pattern_match(
        title,
        (
            r"\b(?:global(?:ly)?|worldwide|world wide)"
            r"[-–—,/|:()\[\]\s]*remote\b(?:\s*[)\]])?",
            r"\bremote[-–—,/|:()\[\]\s]*"
            r"(?:global(?:ly)?|worldwide|world wide)\b(?:\s*[)\]])?",
        ),
    )


_SEMANTIC_REMOTE_TITLE_PATTERNS = (
    r"\bremote[- ]+(?:access|build|control|diagnostics?|monitoring|operations|"
    r"sensing|service|support)\b",
)


def _title_work_arrangement_remote_match(title: str) -> str | None:
    work_arrangement_text = title
    for pattern in _SEMANTIC_REMOTE_TITLE_PATTERNS:
        work_arrangement_text = re.sub(
            pattern, "", work_arrangement_text, flags=re.IGNORECASE
        )
    return _first_pattern_match(
        work_arrangement_text,
        (
            r"^\s*remote\b",
            r"(?:[-–—|:/,(\[]\s*)remote\b",
            r"\bremote\s*(?:[-–—|:/,)\]]|$)",
        ),
    )


def _has_global_remote_claim(text: str) -> bool:
    return _first_pattern_match(text, _GLOBAL_REMOTE_CLAIM_PATTERNS) is not None


def _is_global_scope_value(value: str) -> bool:
    normalised = _normalise_remote_location(value)
    return normalised in {
        "all countries",
        "any country",
        "anywhere",
        "global",
        "globally",
        "world wide",
        "worldwide",
    } or _has_global_remote_claim(value)


def _is_unambiguously_global_remote_location(location: str) -> bool:
    # Provider labels sometimes encode mutually exclusive work-location alternatives with a
    # semicolon. An unqualified worldwide remote alternative remains actionable even when a
    # sibling alternative names an office or narrower region. Qualifiers inside the same segment
    # (for example ``Remote Global (US, EU)``) deliberately fail the full match.
    alternatives = re.split(r"[;|\n]+", location)
    return any(
        re.fullmatch(pattern, _normalise_remote_location(alternative))
        for alternative in alternatives
        for pattern in _GLOBAL_REMOTE_LOCATION_PATTERNS
    )


def _structured_remote_location_is_restricted(location: str) -> bool:
    if not re.search(r"\bremote\b", location):
        return False
    if _matches_any_pattern(location, _PAKISTAN_EXCLUSION_PATTERNS):
        return True
    if _matches_any_pattern(location, _RESTRICTED_REGION_PATTERNS):
        return True
    if _matches_any_pattern(location, _GLOBAL_SCOPE_LIMITATION_PATTERNS):
        return True
    if _is_unambiguously_global_remote_location(location):
        return False
    if re.search(r"\bpakistan\b", location):
        return False
    if _matches_any_pattern(location, _REGIONAL_UNCONFIRMED_PATTERNS):
        return False
    if _matches_any_pattern(location, _TIMEZONE_CONSTRAINT_PATTERNS):
        return False

    normalised = _normalise_remote_location(location)
    return not any(
        re.fullmatch(pattern, normalised) for pattern in _GENERIC_REMOTE_LOCATION_PATTERNS
    )


def _region_eligibility_match(text: str, region_patterns: tuple[str, ...]) -> str | None:
    for region in region_patterns:
        patterns = (
            rf"\bremote(?:ly)?\s+(?:from|in|within|across)\s+(?:the\s+)?{region}",
            rf"\b(?:work|working)\s+remotely\s+(?:from|in|within)\s+(?:the\s+)?{region}",
            rf"\b(?:candidates?|applicants?|employees?|contractors?)\s+(?:must\s+|need\s+to\s+|are\s+required\s+to\s+)?(?:be\s+)?(?:based|located|live|living|reside|residing)\s+(?:in|within)\s+(?:the\s+)?{region}",
            rf"\b(?:be|must\s+be|need\s+to\s+be|required\s+to\s+be)\s+(?:based|located|living|residing)\s+(?:in|within)\s+(?:the\s+)?{region}",
            rf"{region}[- ]based\s+(?:candidates?|applicants?|employees?|contractors?)",
            rf"\b(?:open|available)\s+to\s+(?:the\s+)?{region}[- ]based\s+(?:candidates?|applicants?|employees?)",
            rf"\b(?:candidates?|applicants?|employees?|contractors?)\s+(?:from|across|throughout)\s+(?:the\s+)?{region}",
            rf"\b(?:open|available)\s+to\s+(?:candidates?|applicants?|employees?)\s+(?:based\s+)?(?:in|from)\s+(?:the\s+)?{region}",
            rf"\b(?:hire|hiring|employ)\s+(?:people|employees?|candidates?|talent)?\s*(?:in|from)\s+(?:the\s+)?{region}",
            rf"\b(?:eligible|approved|supported)\s+(?:countries|locations|regions)\b.{{0,80}}{region}",
        )
        match = _first_pattern_match(text, patterns)
        if match:
            return match
    return None


_GENERIC_LOCATION_RESTRICTION_PATTERNS = (
    r"\b(?:candidates?|applicants?|employees?|contractors?)\s+(?:must\s+|need\s+to\s+|are\s+required\s+to\s+)?(?:be\s+)?(?:based|located|live|living|reside|residing)\s+(?:in|within)\s+[a-z][a-z .'-]{1,60}",
    r"\b(?:must|need\s+to|required\s+to)\s+(?:be\s+)?(?:based|located|reside|live)\s+(?:in|within)\s+[a-z][a-z .'-]{1,60}",
    r"\bremote(?:ly)?\s+(?:only\s+)?(?:from|in|within)\s+[a-z][a-z .'-]{1,60}",
    r"\b(?:only\s+)?open\s+to\s+(?:candidates?|applicants?|employees?)\s+(?:based\s+)?(?:in|from)\s+[a-z][a-z .'-]{1,60}",
    r"\b(?:we\s+)?can\s+only\s+(?:hire|employ)\b.{0,60}",
)

_UNRESTRICTED_GLOBAL_DESTINATION_PATTERNS = (
    r"\banywhere\s+in\s+(?:the\s+)?world\b",
    r"\banywhere\b(?!\s+(?:in|within|across)\b)",
    r"\b(?:worldwide|world wide|globally|any\s+country)\b",
)

_LEGAL_WORK_COUNTRY_LIST_PREFIX_PATTERN = re.compile(
    r"\b(?:all\s+)?(?:applicants?|candidates?)\s+must\s+be\s+legally\s+authori[sz]ed"
    r"\s+to\s+work\s+in\s+(?:one\s+of\s+)?(?:the\s+)?following\s+countries\s*:\s*",
    flags=re.IGNORECASE,
)
_LEGAL_WORK_COUNTRY_LIST_SECTION_BOUNDARY = re.compile(
    r"(?:</(?:div|li|p|ul)>|\n|\s+(?=(?:about(?:\s+us)?|benefits|equal\s+opportunity|"
    r"how\s+we|our\s+(?:company|culture|mission|team)|please|responsibilities|the\s+role|"
    r"we\s+(?:are|believe|have|offer)|what\s+(?:to\s+expect|we|you)|who\s+we|you\s+will)\b))",
    flags=re.IGNORECASE,
)
_COUNTRY_LIST_PERIOD_ABBREVIATIONS = re.compile(
    r"\b(?:u\.s\.a|u\.a\.e|e\.u|u\.k|u\.s|st)\.", flags=re.IGNORECASE
)


def _legal_work_country_list(text: str) -> tuple[str, bool] | None:
    prefix = _LEGAL_WORK_COUNTRY_LIST_PREFIX_PATTERN.search(text)
    if not prefix:
        return None
    remainder = text[prefix.end() : prefix.end() + 500]
    # Greenhouse flattens block and list HTML to spaces. Protect common dotted country aliases,
    # then stop at the first real sentence delimiter or recognizable section transition. This
    # prevents a later company-history mention of Pakistan from becoming part of the legal list.
    protected = _COUNTRY_LIST_PERIOD_ABBREVIATIONS.sub(
        lambda match: match.group(0).replace(".", "\u2024"), remainder
    )
    delimiter = re.search(r"[.!?;]", protected)
    section_boundary = _LEGAL_WORK_COUNTRY_LIST_SECTION_BOUNDARY.search(protected)
    boundary_candidates = [
        match.start() for match in (delimiter, section_boundary) if match is not None
    ]
    end = min(boundary_candidates, default=len(remainder))
    countries = re.sub(r"<[^>]+>", " ", remainder[:end])
    countries = re.sub(r"\s+", " ", countries).strip(" .,:-")
    if not countries:
        return None
    return countries, bool(re.search(r"\bpakistan\b", countries, flags=re.IGNORECASE))


def _generic_location_restriction_match(text: str) -> str | None:
    match = _first_pattern_match(text, _GENERIC_LOCATION_RESTRICTION_PATTERNS)
    if not match:
        return None
    if re.search(r"\bpakistan\b", match):
        return None
    if _matches_any_pattern(match, _REGIONAL_UNCONFIRMED_PATTERNS):
        return None
    if _has_global_remote_claim(match) or _matches_any_pattern(
        match, _UNRESTRICTED_GLOBAL_DESTINATION_PATTERNS
    ):
        return None
    return match


def current_opportunity_score(target: dict[str, Any]) -> tuple[int, list[str]]:
    matching_titles = list(target.get("matching_job_titles") or [])
    active_count = int(target.get("managed_active_job_count") or 0)
    managed_matching_count = int(target.get("managed_matching_job_count") or 0)
    if not managed_matching_count:
        if active_count:
            return -12, ["Has active jobs, but none match the target engineering lanes"]
        role_status = str(target.get("role_match_status") or "weak")
        if matching_titles and role_status in ROLE_STATUS_SCORE_ADJUSTMENTS:
            score = ROLE_STATUS_SCORE_ADJUSTMENTS[role_status]
            return score, ["Matching role evidence lacks a complete provider snapshot"]
        return 0, []

    score = 0
    reasons: list[str] = []
    role_status = str(target.get("managed_role_match_status") or "weak")
    if role_status == "strong":
        score += 70
        reasons.append("Has a current strong senior software engineering role")
    elif role_status == "possible":
        score += 40
        reasons.append("Has a current matching engineering possibility")
    elif role_status == "exclude":
        score -= 30

    score += min(managed_matching_count, 10) * 2
    if active_count:
        score += 8
        reasons.append("Backed by a lifecycle-managed complete source snapshot")

    remote_status = str(
        target.get("managed_best_remote_eligibility")
        or target.get("best_remote_eligibility")
        or "no_remote_evidence"
    )
    if remote_status == "pakistan_explicit":
        score += 30
        reasons.append("At least one matching role explicitly includes Pakistan")
    elif remote_status == "global_explicit":
        score += 28
        reasons.append("At least one matching role is explicitly worldwide remote")
    elif remote_status == "regional_unconfirmed":
        score += 6
        reasons.append("A matching role names a region or timezone, but Pakistan is unconfirmed")
    elif remote_status == "remote_unclear":
        score += 4
        reasons.append("At least one matching role is remote with unclear country eligibility")
    elif remote_status == "restricted_remote":
        score -= 8
        reasons.append("Matching remote roles appear geographically restricted")
    elif remote_status == "onsite_explicit":
        score -= 10
        reasons.append("Matching roles explicitly require onsite or hybrid work")
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
    jobs: list[dict[str, Any]] | None = None,
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
    record.update(
        role_focus_record(
            company,
            jobs=jobs,
        )
    )
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
