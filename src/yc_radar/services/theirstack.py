"""Plan, normalize, and import bounded TheirStack job observations."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.engine import Engine

from yc_radar.domain.job_sources import NormalizedJob
from yc_radar.services.candidate_fit import classify_role_text
from yc_radar.services.company_registry import CompanyIdentityConflict, CompanyRegistry
from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    create_schema,
    normalize_company_name,
)
from yc_radar.services.job_source_registry import default_job_source_providers
from yc_radar.services.job_sync_service import JobSyncService
from yc_radar.services.staging import Observation, StagingRepository, normalize_work_url


THEIRSTACK_PROVIDER = "theirstack"
THEIRSTACK_SOURCE_KIND = "job_aggregator"
IMPORTER_VERSION = "1"
FREE_PLAN_PAGE_SIZE = 25
FREE_PLAN_MAX_PAGES = 5
DEFAULT_PREVIEW_PAGES = 4
DEFAULT_RESERVE_SIZE = 50

# TheirStack's official heuristic. It removes explicitly US/Canada-restricted remote jobs but
# does not prove worldwide eligibility; the local classifier remains conservative after import.
US_CANADA_RESTRICTION_PATTERNS = (
    r"(?i)\b(?:must|need to|required to|should)\b[\w ]{0,22}\b(?:located|based|residing|reside|living|live)\b[\w ]{0,10}\b(?:(?-i:U\.?S\.?A?)|united states|canada)\b",
    r"(?i)\b(?:authoriz\w+|eligible|legally)\b[\w ]{0,18}\bwork\b[\w ]{0,8}\b(?:(?-i:U\.?S\.?A?)|united states|canada)\b",
    r"(?i)\b(?:(?-i:U\.?S\.?A?)\s?citizens?|canadian\s+(?:citizens?|residents?))",
    r"(?i)\b(?:(?-i:U\.?S\.?A?)|united states|canad(?:a|ian))[ -](?:only|based\s+(?:candidates?|applicants?|residents?|employees?)|residents?\s+only)\b",
    r"(?i)remote\s?[-–—(/|]{1,3}\s?(?:(?-i:U\.?S\.?A?)|canada)\b",
)

TITLE_EXCLUSION_PATTERNS = (
    r"\b(?:junior|jr\.?|intern(?:ship)?|graduate|new[ -]?grad|entry[ -]?level|trainee|apprentice)\b",
    r"\b(?:engineering|software|technical|technology)\s+(?:manager|director)\b",
    r"\b(?:manager|director|head)\b.{0,40}\b(?:ai|backend|data|front[- ]?end|"
    r"full[- ]?stack|infrastructure|machine learning|platform|software)\s+"
    r"(?:engineer|engineering)\b",
    r"\b(?:director|head|vice president|vp|chief|cto)\b",
    r"\b(?:lead|technical lead|tech lead)\b",
    r"\b(?:qa|quality assurance|sdet|software engineer in test|test engineer)\b",
    r"\b(?:research scientist|researcher)\b",
    r"\b(?:android|ios|mobile|embedded|firmware|game|security|support|solutions?|sales)\b",
    r"\bfreelance\b",
)

# This is deliberately stricter than a generic "remote" match. It reserves part of the paid
# sample for descriptions that explicitly say the role can be performed worldwide; the full
# record is still reclassified locally after reveal.
_GLOBAL_ANYWHERE_DESCRIPTION_PATTERN = (
    r"(?:anywhere\s+in\s+(?:the\s+)?world\b|"
    r"anywhere\b(?![\s,;:()\-–—/|]*(?:in|within|across|where|except|excluding|"
    r"other\s+than|but\s+not|for\s+(?:up\s+to\s+)?\d|"
    r"\d{1,3}\s+(?:days?|weeks?))\b))"
)
GLOBAL_REMOTE_DESCRIPTION_PATTERNS = (
    rf"(?i)\b(?:this|the)\s+(?:job|role|position|opportunity)\s+"
    rf"(?:is|will\s+be|can\s+be|may\s+be)\s+(?:fully\s+)?remote(?:ly)?\s+"
    rf"(?:from\s+)?(?:{_GLOBAL_ANYWHERE_DESCRIPTION_PATTERN}|worldwide|world wide|globally)\b",
    rf"(?i)\bwork\s+remotely\s+from\s+"
    rf"(?:{_GLOBAL_ANYWHERE_DESCRIPTION_PATTERN}|worldwide|world wide|any\s+country)\b",
    rf"(?i)\bwork(?:ing)?\s+from\s+{_GLOBAL_ANYWHERE_DESCRIPTION_PATTERN}",
    r"(?i)\bwork(?:ing)?\s+from\s+any\s+(?:country|location)\b",
    r"(?i)\b(?:job|role|position|work)\s+(?:can|may)\s+be\s+performed\s+from\s+any\s+(?:country|location|place)\b",
    rf"(?i)\bopen\s+to\s+(?:candidates?|applicants?|employees?)\s+(?:based\s+)?"
    rf"(?:{_GLOBAL_ANYWHERE_DESCRIPTION_PATTERN}|worldwide|globally)\b",
    r"(?i)\bopen\s+to\s+(?:candidates?|applicants?|employees?)\s+(?:based\s+)?(?:in|from)\s+any\s+country\b",
    rf"(?i)\b(?:hire|hiring|employ)\s+(?:people|employees?|candidates?|talent)?\s*"
    rf"(?:from|in)\s+(?:{_GLOBAL_ANYWHERE_DESCRIPTION_PATTERN}|any\s+country|"
    rf"the\s+world|worldwide|globally)\b",
    r"(?i)\blocation[- ](?:agnostic|independent)\b",
    r"(?i)\bno\s+(?:geographic|geographical|location|country)\s+restrictions\b",
)

GLOBAL_ENGINEERING_TITLE_PATTERNS = (
    r"\b(?:software|backend|back[- ]end|full[- ]?stack|frontend|front[- ]end|platform|infrastructure|devops|site reliability|data|machine learning|ml|ai|llm)\b.{0,40}\b(?:engineer|developer)\b",
    r"\b(?:engineer|developer)\b.{0,40}\b(?:software|backend|back[- ]end|full[- ]?stack|frontend|front[- ]end|platform|infrastructure|devops|site reliability|data|machine learning|ml|ai|llm)\b",
    r"\b(?:swe|sde|sre|member of technical staff)\b",
)


@dataclass(frozen=True)
class SearchStratum:
    name: str
    weight: int
    posted_at_max_age_days: int
    title_patterns: tuple[str, ...]
    description_patterns: tuple[str, ...] = ()


DEFAULT_SEARCH_STRATA = (
    SearchStratum(
        name="global_explicit",
        weight=40,
        posted_at_max_age_days=21,
        title_patterns=GLOBAL_ENGINEERING_TITLE_PATTERNS,
        description_patterns=GLOBAL_REMOTE_DESCRIPTION_PATTERNS,
    ),
    SearchStratum(
        name="backend",
        weight=25,
        posted_at_max_age_days=14,
        title_patterns=(
            r"\b(?:backend|back[- ]end|api|server[- ]side)\b.{0,32}\b(?:engineer|developer)\b",
            r"\b(?:engineer|developer)\b.{0,32}\b(?:backend|back[- ]end|api|server[- ]side)\b",
        ),
    ),
    SearchStratum(
        name="software",
        weight=25,
        posted_at_max_age_days=14,
        title_patterns=(
            r"\bsoftware\s+(?:engineer|developer)\b",
            r"\bapplication\s+(?:engineer|developer)\b",
        ),
    ),
    SearchStratum(
        name="fullstack",
        weight=20,
        posted_at_max_age_days=14,
        title_patterns=(r"\bfull[- ]?stack\b", r"\bproduct\s+engineer\b"),
    ),
    SearchStratum(
        name="production_ai",
        weight=20,
        posted_at_max_age_days=21,
        title_patterns=(
            r"\b(?:ai|artificial intelligence|machine learning|ml|llm|generative ai|applied ai)\b.{0,32}\b(?:engineer|developer)\b",
            r"\b(?:engineer|developer)\b.{0,32}\b(?:ai|artificial intelligence|machine learning|ml|llm|generative ai|applied ai)\b",
        ),
        description_patterns=(
            r"(?i)\b(?:production|deploy(?:ment)?|serving|inference|api|backend|cloud|mlops|llmops|rag|monitoring|evaluation)\b",
        ),
    ),
    SearchStratum(
        name="data_engineering",
        weight=15,
        posted_at_max_age_days=21,
        title_patterns=(
            r"\b(?:data|analytics)\b.{0,24}\b(?:engineer|developer)\b",
            r"\b(?:engineer|developer)\b.{0,24}\b(?:data platform|data infrastructure|etl|pipelines?)\b",
        ),
        description_patterns=(
            r"(?i)\b(?:etl|elt|data pipelines?|data platform|data infrastructure|ingestion|warehouse|spark|airflow|kafka|dbt)\b",
        ),
    ),
    SearchStratum(
        name="frontend",
        weight=10,
        posted_at_max_age_days=14,
        title_patterns=(
            r"\bfront[- ]?end\b",
            r"\bfrontend\b",
            r"\bweb\s+(?:engineer|developer)\b",
        ),
    ),
    SearchStratum(
        name="platform_infra",
        weight=15,
        posted_at_max_age_days=21,
        title_patterns=(
            r"\b(?:platform|infrastructure|devops|site reliability|sre|cloud)\b.{0,32}\bengineer\b",
            r"\bengineer\b.{0,32}\b(?:platform|infrastructure|devops|site reliability|sre|cloud)\b",
        ),
        description_patterns=(
            r"(?i)\b(?:software|code|backend|api|distributed systems?|python|typescript|javascript|go|golang|rust|java)\b",
        ),
    ),
    SearchStratum(
        name="founding",
        weight=5,
        posted_at_max_age_days=30,
        title_patterns=(r"\bfounding\s+(?:software\s+)?engineer\b",),
        description_patterns=(
            r"(?i)\b(?:software|code|backend|api|full[- ]?stack|data|ai|machine learning|platform)\b",
        ),
    ),
)


@dataclass(frozen=True)
class PreviewSelection:
    selected_job_ids: tuple[int, ...]
    reserve_job_ids: tuple[int, ...]
    selected_by_stratum: dict[str, int]
    candidates_seen: int


@dataclass(frozen=True)
class CompanyResolution:
    company_id: int
    company_source_id: int
    company_created: bool
    matched_by: str
    identity_state: str


@dataclass(frozen=True)
class ImportResult:
    jobs_seen: int
    jobs_imported: int
    jobs_rejected: int
    companies_resolved: int
    companies_created: int
    provisional_companies: int
    source_runs: int
    staging_run_id: int | None
    staging_observations: int
    staging_work_items: int
    errors: tuple[dict[str, str], ...]


def quota_by_stratum(
    credit_budget: int,
    *,
    strata: Sequence[SearchStratum] = DEFAULT_SEARCH_STRATA,
) -> dict[str, int]:
    """Scale the 175-credit role mix with deterministic largest-remainder allocation."""
    if credit_budget < 1:
        raise ValueError("credit_budget must be positive")
    if not strata or any(stratum.weight < 1 for stratum in strata):
        raise ValueError("search strata must have positive weights")
    total_weight = sum(stratum.weight for stratum in strata)
    exact = {
        stratum.name: credit_budget * stratum.weight / total_weight for stratum in strata
    }
    quotas = {name: int(value) for name, value in exact.items()}
    remaining = credit_budget - sum(quotas.values())
    order = sorted(
        strata,
        key=lambda stratum: (-(exact[stratum.name] - quotas[stratum.name]), stratum.name),
    )
    for stratum in order[:remaining]:
        quotas[stratum.name] += 1
    return quotas


def preview_search_body(
    stratum: SearchStratum,
    *,
    page: int,
    excluded_job_ids: Iterable[int] = (),
) -> dict[str, Any]:
    if not 0 <= page < FREE_PLAN_MAX_PAGES:
        raise ValueError("preview page must be between 0 and 4")
    body: dict[str, Any] = {
        "blur_company_data": True,
        "include_total_results": page == 0,
        "limit": FREE_PLAN_PAGE_SIZE,
        "page": page,
        "posted_at_max_age_days": stratum.posted_at_max_age_days,
        "remote": True,
        "is_closed": False,
        "company_type": "direct_employer",
        "employment_statuses_or": ["full_time"],
        "job_seniority_or": ["mid_level", "senior", "staff"],
        "job_title_pattern_or": list(stratum.title_patterns),
        "job_title_pattern_not": list(TITLE_EXCLUSION_PATTERNS),
        "job_description_pattern_not": list(US_CANADA_RESTRICTION_PATTERNS),
        "property_exists_and": ["final_url"],
    }
    if stratum.description_patterns:
        body["job_description_pattern_or"] = list(stratum.description_patterns)
    excluded = sorted({int(job_id) for job_id in excluded_job_ids})
    if excluded:
        body["job_id_not"] = excluded
    return body


def paid_search_body(job_ids: Sequence[int]) -> dict[str, Any]:
    selected = [int(job_id) for job_id in job_ids]
    if not selected or len(selected) > FREE_PLAN_PAGE_SIZE:
        raise ValueError("a paid batch must contain between 1 and 25 job IDs")
    if len(selected) != len(set(selected)):
        raise ValueError("a paid batch cannot contain duplicate job IDs")
    return {
        "posted_at_max_age_days": 90,
        "is_closed": False,
        "job_id_or": selected,
        "include_total_results": False,
        "limit": len(selected),
        "page": 0,
    }


def select_preview_jobs(
    previews: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    credit_budget: int,
    reserve_size: int = DEFAULT_RESERVE_SIZE,
    excluded_job_ids: Iterable[int] = (),
    strata: Sequence[SearchStratum] = DEFAULT_SEARCH_STRATA,
) -> PreviewSelection:
    """Select a fresh, role-balanced, company-diverse paid set from blurred previews."""
    if reserve_size < 0:
        raise ValueError("reserve_size must be non-negative")
    quotas = quota_by_stratum(credit_budget, strata=strata)
    excluded = {int(job_id) for job_id in excluded_job_ids}
    candidates: dict[int, dict[str, Any]] = {}
    memberships: dict[int, set[str]] = defaultdict(set)
    for stratum in strata:
        for raw in previews.get(stratum.name, ()):  # duplicate IDs across pages/strata are normal
            candidate = dict(raw)
            job_id = _job_id(candidate)
            if job_id is None or job_id in excluded:
                continue
            title = _optional_text(candidate.get("job_title"))
            if title is None:
                continue
            company_id = _optional_text(_company_object(candidate).get("id"))
            if company_id is None:
                continue
            if candidate.get("hybrid") is True:
                continue
            employment_statuses = {
                value.casefold() for value in _string_list(candidate.get("employment_statuses"))
            }
            if employment_statuses and employment_statuses != {"full_time"}:
                continue
            if any(
                re.search(pattern, title, flags=re.IGNORECASE)
                for pattern in TITLE_EXCLUSION_PATTERNS
            ):
                continue
            seniority = (_optional_text(candidate.get("seniority")) or "").casefold()
            classification = classify_role_text(
                title,
                _preview_context(candidate),
                seniority=seniority,
            )
            if classification.status == "exclude" or (
                classification.status == "weak"
                and seniority not in {"mid_level", "senior", "staff"}
            ):
                continue
            prior = candidates.get(job_id)
            if prior is None or _preview_score(candidate) > _preview_score(prior):
                candidates[job_id] = candidate
            memberships[job_id].add(stratum.name)

    ranked_by_stratum = {
        stratum.name: sorted(
            (
                candidate
                for job_id, candidate in candidates.items()
                if stratum.name in memberships[job_id]
            ),
            key=_preview_sort_key,
        )
        for stratum in strata
    }
    selected: list[int] = []
    selected_set: set[int] = set()
    selected_by_stratum = {stratum.name: 0 for stratum in strata}
    company_counts: dict[str, int] = defaultdict(int)
    staff_count = 0
    staff_cap = max(1, int(credit_budget * 0.15))

    def add(candidate: Mapping[str, Any], stratum_name: str, *, company_cap: int | None) -> bool:
        nonlocal staff_count
        job_id = _job_id(candidate)
        assert job_id is not None
        if job_id in selected_set:
            return False
        company_key = _preview_company_key(candidate, job_id)
        if company_cap is not None and company_counts[company_key] >= company_cap:
            return False
        if _is_staff_candidate(candidate) and staff_count >= staff_cap:
            return False
        selected.append(job_id)
        selected_set.add(job_id)
        selected_by_stratum[stratum_name] += 1
        company_counts[company_key] += 1
        if _is_staff_candidate(candidate):
            staff_count += 1
        return True

    # Breadth first: one job per employer while each role lane receives its target share.
    for stratum in strata:
        quota = quotas[stratum.name]
        for candidate in ranked_by_stratum[stratum.name]:
            if selected_by_stratum[stratum.name] >= quota or len(selected) >= credit_budget:
                break
            add(candidate, stratum.name, company_cap=1)

    global_ranked = sorted(candidates.values(), key=_preview_sort_key)
    for company_cap in (2, None):
        for candidate in global_ranked:
            if len(selected) >= credit_budget:
                break
            job_id = _job_id(candidate)
            assert job_id is not None
            lane = min(
                memberships[job_id],
                key=lambda name: (selected_by_stratum.get(name, 0) - quotas.get(name, 0), name),
            )
            add(candidate, lane, company_cap=company_cap)
        if len(selected) >= credit_budget:
            break

    reserve: list[int] = []
    reserve_company_counts = dict(company_counts)
    for company_cap in (2, None):
        for candidate in global_ranked:
            if len(reserve) >= reserve_size:
                break
            job_id = _job_id(candidate)
            assert job_id is not None
            if job_id in selected_set or job_id in reserve:
                continue
            company_key = _preview_company_key(candidate, job_id)
            if company_cap is not None and reserve_company_counts.get(company_key, 0) >= company_cap:
                continue
            reserve.append(job_id)
            reserve_company_counts[company_key] = reserve_company_counts.get(company_key, 0) + 1
        if len(reserve) >= reserve_size:
            break

    return PreviewSelection(
        selected_job_ids=tuple(selected),
        reserve_job_ids=tuple(reserve),
        selected_by_stratum=selected_by_stratum,
        candidates_seen=len(candidates),
    )


def normalize_theirstack_job(payload: Mapping[str, Any]) -> NormalizedJob:
    """Map one full TheirStack result into the provider-neutral observation contract."""
    job_id = _job_id(payload)
    title = _optional_text(payload.get("job_title"))
    if job_id is None or title is None:
        raise ValueError("TheirStack job requires id and job_title")
    if payload.get("is_closed") is True or _optional_text(payload.get("closed_at")):
        raise ValueError(f"TheirStack job {job_id} is closed")
    company = _company_object(payload)
    if _optional_text(company.get("id")) is None:
        raise ValueError(f"TheirStack job {job_id} has no stable company ID")

    final_url = _safe_public_url(payload.get("final_url"))
    job_url = _safe_public_url(payload.get("url"))
    source_url = _safe_public_url(payload.get("source_url"))
    posting_url = final_url or job_url or source_url
    apply_url = next(
        (value for value in (job_url, final_url, source_url) if value and value != posting_url),
        posting_url,
    )
    location_records = _location_records(payload)
    primary_location = location_records[0] if location_records else None
    secondary_locations = location_records[1:]
    raw_location = _first_text(
        payload.get("location"),
        payload.get("long_location"),
        payload.get("short_location"),
    )
    location = " / ".join(
        _dedupe_strings(
            [
                raw_location,
                *(record.get("label") for record in location_records),
            ]
        )
    ) or None
    countries = _dedupe_strings(
        [
            *_string_list(payload.get("countries")),
            *(record.get("country") for record in location_records),
        ]
    )
    remote = payload.get("remote") if isinstance(payload.get("remote"), bool) else None
    hybrid = payload.get("hybrid") if isinstance(payload.get("hybrid"), bool) else None
    workplace_type = "hybrid" if hybrid else "remote" if remote else "on_site" if remote is False else None
    employment_statuses = _string_list(payload.get("employment_statuses"))
    structured_evidence = {
        "schema_version": 1,
        "provider": THEIRSTACK_PROVIDER,
        "requisition_id": _explicit_requisition_id(payload),
        "workplace": _compact_mapping(
            type=workplace_type,
            is_remote=remote,
            scope_kind="posting_location_unverified",
        ),
        "primary_location": primary_location,
        "secondary_locations": secondary_locations,
        "countries": countries,
        "provider_metadata": {
            "seniority": _optional_text(payload.get("seniority")),
            "technology_slugs": _string_list(payload.get("technology_slugs")),
            "keyword_slugs": _string_list(payload.get("keyword_slugs")),
            "salary": _compact_mapping(
                currency=_optional_text(payload.get("salary_currency")),
                text=_optional_text(payload.get("salary_string")),
                min_annual_usd=payload.get("min_annual_salary_usd"),
                max_annual_usd=payload.get("max_annual_salary_usd"),
            ),
            "discovered_at": _optional_text(payload.get("discovered_at")),
            "reposted": payload.get("reposted") if isinstance(payload.get("reposted"), bool) else None,
        },
        "eligibility_signals": [
            {"kind": "vendor_field", "name": "remote", "value": remote},
            {"kind": "vendor_field", "name": "hybrid", "value": hybrid},
            {
                "kind": "local_assessment",
                "name": "applicant geography",
                "value": "unverified unless the description states eligibility",
            },
        ],
        "application": _compact_mapping(
            posting_url=posting_url,
            apply_url=apply_url,
            final_url=final_url,
            source_url=source_url,
        ),
        "vendor_identity": {
            "job_id": str(job_id),
            "company_id": str(company["id"]),
        },
    }
    content = {
        "title": title,
        "posting_url": posting_url,
        "apply_url": apply_url,
        "description_text": _optional_text(payload.get("description")),
        "location": location,
        "employment_type": ", ".join(employment_statuses) or None,
        "structured_evidence": structured_evidence,
    }
    return NormalizedJob(
        external_job_id=str(job_id),
        title=title,
        posting_url=posting_url,
        apply_url=apply_url,
        description_text=content["description_text"],
        location=location,
        employment_type=content["employment_type"],
        source_published_at=_parse_timestamp(payload.get("date_posted")),
        source_updated_at=_parse_timestamp(payload.get("date_reposted")),
        content_hash=hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest(),
        structured_evidence=structured_evidence,
        raw_payload=_json_safe(payload),
    )


def resolve_theirstack_company(
    engine: Engine,
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> CompanyResolution:
    """Resolve a stable vendor company identity without merging on a weak name match."""
    company = _company_object(payload)
    external_id = _required_text(company.get("id"), "TheirStack company ID")
    name = _required_text(company.get("name"), f"TheirStack company {external_id} name")
    observed_at = now or datetime.now(UTC)
    metadata = {
        "provider_name": "TheirStack",
        "company": {
            key: value
            for key, value in {
                "id": external_id,
                "name": name,
                "domain": _optional_text(company.get("domain")),
                "linkedin_url": _safe_public_url(company.get("linkedin_url")),
                "country": _optional_text(company.get("country")),
                "country_code": _optional_text(company.get("country_code")),
                "employee_count": company.get("employee_count"),
                "yc_batch": _optional_text(company.get("yc_batch")),
            }.items()
            if value is not None
        },
    }
    with engine.connect() as connection:
        existing_source = (
            connection.execute(
                select(company_sources_table).where(
                    company_sources_table.c.provider == THEIRSTACK_PROVIDER,
                    company_sources_table.c.external_id == external_id,
                )
            )
            .mappings()
            .first()
        )
    registry = CompanyRegistry(engine)
    if existing_source is not None:
        company_id = int(existing_source["company_id"])
        registry.register_source_identity(
            company_id=company_id,
            provider=THEIRSTACK_PROVIDER,
            external_id=external_id,
            source_kind=THEIRSTACK_SOURCE_KIND,
            sync_mode="observation",
            metadata=metadata,
            now=observed_at,
        )
        with engine.connect() as connection:
            identity_state = str(
                connection.scalar(
                    select(companies_table.c.identity_state).where(
                        companies_table.c.id == company_id
                    )
                )
            )
        return CompanyResolution(
            company_id=company_id,
            company_source_id=int(existing_source["id"]),
            company_created=False,
            matched_by="theirstack_external_id",
            identity_state=identity_state,
        )

    domain = _optional_text(company.get("domain"))
    website = _company_website(domain)
    registration = None
    conflict_reason = None
    if website is not None:
        try:
            registration = registry.register_company(
                name=name,
                website=website,
                requested_slug=_company_slug(name, external_id),
                now=observed_at,
            )
        except (CompanyIdentityConflict, ValueError) as exc:
            conflict_reason = type(exc).__name__

    if registration is None:
        requested_slug = _provisional_slug(external_id)
        with engine.connect() as connection:
            orphan = (
                connection.execute(
                    select(companies_table).where(companies_table.c.slug == requested_slug)
                )
                .mappings()
                .first()
            )
        if (
            orphan is not None
            and orphan["identity_state"] == "provisional"
            and orphan["normalized_name"] == normalize_company_name(name)
        ):
            registration = _existing_registration(int(orphan["id"]), conflict_reason)
        else:
            registration = registry.register_provisional_company(
                name=name,
                requested_slug=requested_slug,
                now=observed_at,
            )
    if conflict_reason:
        metadata["identity_conflict"] = conflict_reason
    registry.register_source_identity(
        company_id=registration.company_id,
        provider=THEIRSTACK_PROVIDER,
        external_id=external_id,
        source_kind=THEIRSTACK_SOURCE_KIND,
        sync_mode="observation",
        metadata=metadata,
        now=observed_at,
    )
    with engine.connect() as connection:
        source = (
            connection.execute(
                select(company_sources_table).where(
                    company_sources_table.c.provider == THEIRSTACK_PROVIDER,
                    company_sources_table.c.external_id == external_id,
                )
            )
            .mappings()
            .one()
        )
        identity_state = str(
            connection.scalar(
                select(companies_table.c.identity_state).where(
                    companies_table.c.id == registration.company_id
                )
            )
        )
    return CompanyResolution(
        company_id=registration.company_id,
        company_source_id=int(source["id"]),
        company_created=registration.company_created,
        matched_by=registration.matched_by,
        identity_state=identity_state,
    )


def import_theirstack_jobs(
    engine: Engine,
    jobs: Sequence[Mapping[str, Any]],
    *,
    plan_id: str,
    stage_urls: bool = True,
    now: datetime | None = None,
) -> ImportResult:
    """Persist raw URL evidence and observation-mode jobs with per-company replay keys."""
    if not re.fullmatch(r"[0-9a-f]{64}", plan_id):
        raise ValueError("plan_id must be a lowercase SHA-256 digest")
    create_schema(engine)
    observed_at = now or datetime.now(UTC)
    grouped: dict[int, list[NormalizedJob]] = defaultdict(list)
    resolutions: dict[int, CompanyResolution] = {}
    observations: list[Observation] = []
    errors: list[dict[str, str]] = []
    seen_job_ids: set[int] = set()

    for index, raw in enumerate(jobs):
        payload = dict(raw)
        job_id = _job_id(payload)
        if job_id is None:
            errors.append({"kind": "invalid_job", "message": f"row {index} has no integer ID"})
            continue
        if job_id in seen_job_ids:
            continue
        seen_job_ids.add(job_id)
        resolution = None
        try:
            normalized = normalize_theirstack_job(payload)
            resolution = resolve_theirstack_company(engine, payload, now=observed_at)
        except Exception as exc:
            errors.append(
                {
                    "kind": type(exc).__name__,
                    "message": f"job {job_id}: {str(exc)[:400]}",
                }
            )
        else:
            resolutions[job_id] = resolution
            grouped[resolution.company_source_id].append(normalized)

        observation_url = _observation_url(payload, stage_urls=stage_urls)
        observation_payload: dict[str, Any] = {
            "schema_version": 1,
            "provider": THEIRSTACK_PROVIDER,
            "plan_id": plan_id,
            "theirstack_job": _json_safe(payload),
        }
        company_name = _optional_text(_company_object(payload).get("name"))
        if company_name:
            observation_payload["company_name"] = company_name
        if resolution is not None:
            # This is explicitly derived local identity, not a vendor company ID.
            observation_payload["company_id"] = resolution.company_id
        observations.append(
            Observation(
                url=observation_url,
                observation_key=f"theirstack:{plan_id}:{job_id}",
                payload=observation_payload,
                observed_at=observed_at,
                priority=_staging_priority(payload),
            )
        )

    staging_run_id = None
    staging_inserted = 0
    staging_work_items = 0
    if observations:
        load = StagingRepository(engine).load(
            run_key=f"theirstack:{plan_id}",
            source=THEIRSTACK_PROVIDER,
            observations=observations,
        )
        staging_run_id = load.run_id
        staging_inserted = load.observations_inserted
        staging_work_items = load.work_items_inserted

    service = JobSyncService(engine, clock=lambda: observed_at)
    source_runs = 0
    imported = 0
    for source_id in sorted(grouped):
        source_jobs = grouped[source_id]
        result = service.sync_observations(
            company_source_id=source_id,
            run_key=f"theirstack:{plan_id}:{source_id}",
            jobs=source_jobs,
            adapter_version=IMPORTER_VERSION,
        )
        if result.status != "completed":
            errors.append(
                {
                    "kind": "observation_sync_failed",
                    "message": f"source {source_id} finished as {result.status}",
                }
            )
            continue
        source_runs += 1
        imported += result.jobs_fetched

    unique_companies = {resolution.company_id for resolution in resolutions.values()}
    created_companies = {
        resolution.company_id
        for resolution in resolutions.values()
        if resolution.company_created
    }
    provisional_companies = {
        resolution.company_id
        for resolution in resolutions.values()
        if resolution.identity_state == "provisional"
    }
    return ImportResult(
        jobs_seen=len(seen_job_ids),
        jobs_imported=imported,
        jobs_rejected=len(seen_job_ids) - imported,
        companies_resolved=len(unique_companies),
        companies_created=len(created_companies),
        provisional_companies=len(provisional_companies),
        source_runs=source_runs,
        staging_run_id=staging_run_id,
        staging_observations=staging_inserted,
        staging_work_items=staging_work_items,
        errors=tuple(errors),
    )


def import_result_dict(result: ImportResult) -> dict[str, Any]:
    return asdict(result)


def plan_digest(payload: Mapping[str, Any]) -> str:
    """Hash immutable plan scope while ignoring runtime progress and timestamps."""
    scope = {
        "schema_version": payload.get("schema_version"),
        "importer_version": payload.get("importer_version"),
        "observation_time": payload.get("observation_time"),
        "credit_budget": payload.get("credit_budget"),
        "excluded_job_ids": payload.get("excluded_job_ids"),
        "selected_job_ids": payload.get("selected_job_ids"),
        "reserve_job_ids": payload.get("reserve_job_ids"),
        "strata": payload.get("strata"),
    }
    return hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _preview_context(payload: Mapping[str, Any]) -> str:
    return " ".join(
        _dedupe_strings(
            [
                _optional_text(payload.get("location")),
                *_string_list(payload.get("technology_slugs")),
                *_string_list(payload.get("keyword_slugs")),
            ]
        )
    )


def _preview_score(payload: Mapping[str, Any]) -> int:
    score = 0
    title = _optional_text(payload.get("job_title")) or ""
    seniority = (_optional_text(payload.get("seniority")) or "").casefold()
    classification = classify_role_text(
        title,
        _preview_context(payload),
        seniority=seniority,
    )
    score += {"strong": 40, "possible": 24, "weak": 12}.get(classification.status, 0)
    score += {"senior": 8, "mid_level": 6, "staff": 4}.get(seniority, 0)
    preferred_tech = {
        "aws",
        "typescript",
        "python",
        "kubernetes",
        "react",
        "postgresql",
        "gcp",
        "docker",
        "node-js",
        "nodejs",
        "airflow",
        "dbt",
        "kafka",
        "spark",
    }
    score += min(10, len(preferred_tech & set(_string_list(payload.get("technology_slugs")))) * 2)
    posted = _parse_timestamp(payload.get("date_posted"))
    if posted is not None:
        age = max(0, (datetime.now(UTC).date() - posted.date()).days)
        score += max(0, 10 - min(age, 10))
    return score


def _preview_sort_key(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    posted = _parse_timestamp(payload.get("date_posted"))
    return (
        -_preview_score(payload),
        -(posted.timestamp() if posted else 0.0),
        _job_id(payload) or 0,
    )


def _preview_company_key(payload: Mapping[str, Any], job_id: int) -> str:
    company = _company_object(payload)
    for value in (
        company.get("id"),
        payload.get("company_id"),
        company.get("domain"),
        company.get("name"),
    ):
        normalized = _optional_text(value)
        if normalized:
            return normalized.casefold()
    return f"unknown-job:{job_id}"


def _is_staff_candidate(payload: Mapping[str, Any]) -> bool:
    seniority = (_optional_text(payload.get("seniority")) or "").casefold()
    title = _optional_text(payload.get("job_title")) or ""
    return seniority == "staff" or bool(re.search(r"\b(?:staff|principal)\b", title, re.I))


def _company_object(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("company_object")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _job_id(payload: Mapping[str, Any]) -> int | None:
    raw = payload.get("id")
    if isinstance(raw, bool):
        return None
    try:
        result = int(raw)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _company_website(domain: str | None) -> str | None:
    if domain is None:
        return None
    raw = domain.strip()
    if not raw or "@" in raw:
        return None
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    return candidate if parsed.scheme in {"http", "https"} and parsed.hostname else None


def _company_slug(name: str, external_id: str) -> str:
    name_slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    suffix = hashlib.sha256(external_id.encode()).hexdigest()[:8]
    return f"{name_slug or 'company'}-{suffix}"


def _provisional_slug(external_id: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "-", external_id.casefold()).strip("-")
    if not suffix or len(suffix) > 80:
        suffix = hashlib.sha256(external_id.encode()).hexdigest()[:16]
    return f"theirstack-{suffix}"


def _existing_registration(company_id: int, conflict_reason: str | None):
    from yc_radar.services.company_registry import CompanyRegistrationResult

    return CompanyRegistrationResult(
        company_id=company_id,
        company_created=False,
        matched_by=(
            f"provisional_after_{conflict_reason}" if conflict_reason else "provisional_slug"
        ),
    )


def _observation_url(payload: Mapping[str, Any], *, stage_urls: bool) -> str:
    urls = [
        value
        for value in (
            _safe_public_url(payload.get("final_url")),
            _safe_public_url(payload.get("url")),
            _safe_public_url(payload.get("source_url")),
        )
        if value
    ]
    if not stage_urls:
        return ""
    providers = default_job_source_providers()
    for url in urls:
        detected = providers.detect(url)
        if detected is not None:
            return detected.canonical_url
    return urls[0] if urls else ""


def _staging_priority(payload: Mapping[str, Any]) -> int:
    for value in (
        payload.get("final_url"),
        payload.get("url"),
        payload.get("source_url"),
    ):
        url = _safe_public_url(value)
        if url and default_job_source_providers().detect(url) is not None:
            return 100
    return 0


def _safe_public_url(value: Any) -> str | None:
    raw = _optional_text(value)
    if raw is None:
        return None
    try:
        normalize_work_url(raw)
    except ValueError:
        return None
    return raw


def _location_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("locations")
    if not isinstance(raw, list):
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, Mapping):
            continue
        record = _compact_mapping(
            label=_first_text(value.get("display_name"), value.get("name")),
            locality=_optional_text(value.get("name")),
            region=_first_text(value.get("state"), value.get("admin1_name")),
            country=_optional_text(value.get("country_name")),
            country_code=_optional_text(value.get("country_code")),
            provider_location_id=value.get("id"),
        )
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
        if record and encoded not in seen:
            seen.add(encoded)
            records.append(record)
    return records


def _explicit_requisition_id(payload: Mapping[str, Any]) -> str | None:
    for key in (
        "requisition_id",
        "requisitionId",
        "requisition_number",
        "requisitionNumber",
        "req_id",
        "reqId",
        "job_code",
        "jobCode",
    ):
        if value := _optional_text(payload.get(key)):
            return value
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _compact_mapping(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _required_text(value: Any, label: str) -> str:
    result = _optional_text(value)
    if result is None:
        raise ValueError(f"{label} is required")
    return result


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _first_text(*values: Any) -> str | None:
    return next((result for value in values if (result := _optional_text(value))), None)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return _dedupe_strings(value)


def _dedupe_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _optional_text(value)
        if normalized is None or normalized.casefold() in seen:
            continue
        seen.add(normalized.casefold())
        result.append(normalized)
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
