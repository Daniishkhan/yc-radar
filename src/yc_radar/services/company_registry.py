from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from yc_radar.domain.job_sources import NormalizedJob, SourceSnapshot, SyncResult
from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    create_schema,
    jobs_table,
    normalize_company_name,
    primary_domain_for_website,
    sanitized_yc_company_website,
)
from yc_radar.services.job_repository import JobRepository
from yc_radar.services.job_sync_service import JobSyncService


YC_JOB_ADAPTER_VERSION = "1"


class CompanyIdentityConflict(ValueError):
    """Raised when identity evidence cannot safely resolve to one neutral company."""


@dataclass(frozen=True)
class CompanyRegistrationResult:
    company_id: int
    company_created: bool
    matched_by: str


class CompanyRegistry:
    """Own canonical companies and the provider sources attached to them."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.source_repository = JobRepository(engine)

    def register_company(
        self,
        *,
        name: str,
        website: str,
        requested_slug: str | None = None,
        now: datetime | None = None,
    ) -> CompanyRegistrationResult:
        create_schema(self.engine)
        company_name = name.strip()
        if not company_name:
            raise ValueError("company name is required")
        sanitized_website = sanitized_yc_company_website(
            {"name": company_name, "website": website}
        )
        if sanitized_website is None:
            raise ValueError("website is not safe company-owned identity evidence")
        domain = verified_primary_domain(sanitized_website)
        normalized_name = normalize_company_name(company_name)
        observed_at = now or datetime.now(UTC)
        with self.engine.begin() as connection:
            domain_matches = list(
                connection.execute(
                    select(companies_table).where(companies_table.c.primary_domain == domain)
                ).mappings()
            )
            name_matches = list(
                connection.execute(
                    select(companies_table).where(
                        companies_table.c.normalized_name == normalized_name
                    )
                ).mappings()
            )
            if len(domain_matches) > 1 or len(name_matches) > 1:
                raise CompanyIdentityConflict("verified domain or normalized name is ambiguous")
            if (
                domain_matches
                and name_matches
                and int(domain_matches[0]["id"]) != int(name_matches[0]["id"])
            ):
                raise CompanyIdentityConflict(
                    "verified domain and normalized name identify different companies"
                )
            if domain_matches:
                existing = domain_matches[0]
                if existing["normalized_name"] != normalized_name:
                    raise CompanyIdentityConflict(
                        "verified primary domain belongs to a company with a different "
                        "normalized name"
                    )
                connection.execute(
                    update(companies_table)
                    .where(companies_table.c.id == existing["id"])
                    .values(
                        website=sanitized_website,
                        primary_domain=domain,
                        identity_state="verified",
                        updated_at=observed_at,
                    )
                )
                return CompanyRegistrationResult(
                    company_id=int(existing["id"]),
                    company_created=False,
                    matched_by="primary_domain",
                )

            slug = next_available_slug(
                connection,
                requested_slug=requested_slug,
                company_name=company_name,
                domain=domain,
            )
            company_id = int(
                connection.execute(
                    insert(companies_table)
                    .values(
                        name=company_name,
                        normalized_name=normalized_name,
                        slug=slug,
                        website=sanitized_website,
                        primary_domain=domain,
                        identity_state="verified",
                        created_at=observed_at,
                        updated_at=observed_at,
                    )
                    .returning(companies_table.c.id)
                ).scalar_one()
            )
        return CompanyRegistrationResult(
            company_id=company_id,
            company_created=True,
            matched_by="new_company",
        )

    def register_provisional_company(
        self,
        *,
        name: str,
        requested_slug: str | None = None,
        now: datetime | None = None,
    ) -> CompanyRegistrationResult:
        """Create an unresolved company without guessing an identity match.

        A provider-confirmed company name is useful even when the provider exposes no safe
        company-owned domain. Name-only matching is deliberately forbidden here: two unrelated
        companies may share a normalized name. The source registry subsequently attaches the
        provider's unique external identity, which makes retries idempotent at the source layer.
        """
        create_schema(self.engine)
        company_name = name.strip()
        if not company_name:
            raise ValueError("company name is required")
        observed_at = now or datetime.now(UTC)
        with self.engine.begin() as connection:
            slug = next_available_slug(
                connection,
                requested_slug=requested_slug,
                company_name=company_name,
                domain=None,
            )
            company_id = int(
                connection.execute(
                    insert(companies_table)
                    .values(
                        name=company_name,
                        normalized_name=normalize_company_name(company_name),
                        slug=slug,
                        website=None,
                        primary_domain=None,
                        identity_state="provisional",
                        created_at=observed_at,
                        updated_at=observed_at,
                    )
                    .returning(companies_table.c.id)
                ).scalar_one()
            )
        return CompanyRegistrationResult(
            company_id=company_id,
            company_created=True,
            matched_by="provisional_company",
        )

    def register_source_identity(
        self,
        *,
        company_id: int,
        provider: str,
        external_id: str,
        source_kind: str = "directory",
        source_url: str | None = None,
        sync_mode: str = "none",
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Attach a provider identity to a company without changing company ownership."""
        create_schema(self.engine)
        normalized_provider = provider.strip().lower()
        normalized_external_id = external_id.strip()
        normalized_source_kind = source_kind.strip().lower()
        normalized_sync_mode = sync_mode.strip().lower()
        if not normalized_provider or not normalized_external_id:
            raise ValueError("provider and external_id are required")
        if not normalized_source_kind or not normalized_sync_mode:
            raise ValueError("source_kind and sync_mode are required")
        observed_at = now or datetime.now(UTC)
        with self.engine.connect() as connection:
            company_exists = connection.scalar(
                select(companies_table.c.id).where(companies_table.c.id == company_id)
            )
            if company_exists is None:
                raise ValueError(f"unknown company_id: {company_id}")
        source, allowed, created = self.source_repository.register_source(
            company_id=company_id,
            provider=normalized_provider,
            source_kind=normalized_source_kind,
            external_id=normalized_external_id,
            source_url=source_url,
            sync_mode=normalized_sync_mode,
            now=observed_at,
            metadata=metadata,
        )
        if not allowed:
            raise CompanyIdentityConflict(
                f"{normalized_provider} identity {normalized_external_id} belongs "
                f"to company_id={source['company_id']}"
            )
        return created


def sync_yc_job_snapshots(
    engine: Engine,
    jobs: list[dict[str, Any]],
    *,
    complete_company_slugs: set[str],
    observed_at: datetime | None = None,
) -> list[SyncResult]:
    """Normalize YC jobs and apply complete per-company snapshots through one lifecycle service."""
    create_schema(engine)
    now = observed_at or datetime.now(UTC)
    complete_slugs = {
        str(slug).strip().lower() for slug in complete_company_slugs if str(slug).strip()
    }
    with engine.connect() as connection:
        source_rows = [
            dict(row)
            for row in connection.execute(
                select(company_sources_table).where(company_sources_table.c.provider == "yc")
            ).mappings()
        ]
        existing_job_source_ids = {
            int(source_id)
            for source_id in connection.scalars(
                select(jobs_table.c.company_source_id)
                .join(
                    company_sources_table,
                    company_sources_table.c.id == jobs_table.c.company_source_id,
                )
                .where(company_sources_table.c.provider == "yc")
                .distinct()
            )
        }

    sources_by_external_id = {str(source["external_id"]): source for source in source_rows}
    sources_by_slug: dict[str, dict[str, Any]] = {}
    for source in source_rows:
        slug = _yc_source_slug(source)
        if slug in sources_by_slug and int(sources_by_slug[slug]["id"]) != int(source["id"]):
            raise CompanyIdentityConflict(f"YC slug {slug!r} belongs to multiple companies")
        sources_by_slug[slug] = source

    unknown_complete_slugs = complete_slugs - set(sources_by_slug)
    if unknown_complete_slugs:
        sample = ", ".join(sorted(unknown_complete_slugs)[:5])
        raise ValueError(f"Complete YC snapshot references unknown company slugs: {sample}")

    jobs_by_source: dict[int, list[NormalizedJob]] = {}
    source_by_id: dict[int, dict[str, Any]] = {}
    for payload in jobs:
        source = _resolve_yc_source(
            payload,
            sources_by_external_id=sources_by_external_id,
            sources_by_slug=sources_by_slug,
        )
        source_id = int(source["id"])
        source_slug = _yc_source_slug(source)
        if source_slug not in complete_slugs:
            raise ValueError(
                f"YC job {payload.get('id')!r} belongs to incomplete source {source_slug!r}"
            )
        source_by_id[source_id] = source
        jobs_by_source.setdefault(source_id, []).append(normalize_yc_job(payload))

    complete_source_ids = {
        int(sources_by_slug[slug]["id"])
        for slug in complete_slugs
        if slug in sources_by_slug
    }
    sources_to_sync = set(jobs_by_source) | (complete_source_ids & existing_job_source_ids)
    service = JobSyncService(engine, clock=lambda: now)
    results: list[SyncResult] = []
    for source_id in sorted(sources_to_sync):
        source = source_by_id.get(source_id)
        if source is None:
            source = next(row for row in source_rows if int(row["id"]) == source_id)
        snapshot = SourceSnapshot(
            provider="yc",
            external_source_id=str(source["external_id"]),
            adapter_version=YC_JOB_ADAPTER_VERSION,
            is_complete=True,
            http_status=200,
            jobs=jobs_by_source.get(source_id, []),
            request_metadata={"source": "yc_company_page"},
        )
        results.append(
            service.sync_snapshot(
                company_source_id=source_id,
                run_key=f"yc:{now.isoformat()}",
                snapshot=snapshot,
            )
        )
    return results


def normalize_yc_job(payload: dict[str, Any]) -> NormalizedJob:
    """Convert one public YC posting to the provider-neutral job contract."""
    external_job_id = _required_text(payload.get("id") or payload.get("objectID"), "job id")
    title = _required_text(payload.get("title"), f"YC job {external_job_id} title")
    relative_url = _optional_text(payload.get("url"))
    posting_url = (
        urljoin("https://www.ycombinator.com", relative_url) if relative_url else None
    )
    apply_url = _optional_text(payload.get("applyUrl"))
    location = _optional_text(payload.get("location"))
    department = _first_text(
        payload.get("roleSpecificType"),
        payload.get("prettyRole"),
        payload.get("role"),
    )
    employment_type = _optional_text(payload.get("type"))
    description_text = " ".join(
        value
        for value in (
            _optional_text(payload.get("roleSpecificType")),
            _optional_text(payload.get("prettyRole")),
            _optional_text(payload.get("visa")),
        )
        if value
    ) or None
    structured_evidence = {
        "schema_version": 1,
        "provider": "yc",
        "requisition_id": _first_text(
            payload.get("requisition_id"),
            payload.get("requisitionId"),
            payload.get("requisitionNumber"),
            payload.get("reqId"),
            payload.get("jobCode"),
        ),
        "role": _optional_text(payload.get("role")),
        "role_specific_type": _optional_text(payload.get("roleSpecificType")),
        "pretty_role": _optional_text(payload.get("prettyRole")),
        "salary_range": _optional_text(payload.get("salaryRange")),
        "equity_range": _optional_text(payload.get("equityRange")),
        "min_experience": _optional_text(payload.get("minExperience")),
        "min_school_year": _optional_text(payload.get("minSchoolYear")),
        "visa": _optional_text(payload.get("visa")),
        "skills": _string_list(payload.get("skills")),
        "is_incomplete": bool(payload.get("isIncomplete")),
        "created_at_text": _optional_text(payload.get("createdAt")),
        "last_active_text": _optional_text(payload.get("lastActive")),
    }
    content = {
        "title": title,
        "posting_url": posting_url,
        "apply_url": apply_url,
        "description_text": description_text,
        "location": location,
        "department": department,
        "employment_type": employment_type,
        "structured_evidence": structured_evidence,
    }
    return NormalizedJob(
        external_job_id=external_job_id,
        title=title,
        posting_url=posting_url,
        apply_url=apply_url,
        description_text=description_text,
        location=location,
        department=department,
        employment_type=employment_type,
        source_published_at=_optional_datetime(payload.get("createdAt")),
        source_updated_at=_optional_datetime(payload.get("updatedAt")),
        content_hash=hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        structured_evidence=structured_evidence,
        raw_payload=_json_safe(payload),
    )


def verified_primary_domain(website: str) -> str:
    try:
        parsed = urlparse(website)
    except ValueError as exc:
        raise ValueError("website must be an absolute http(s) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("website must be an absolute http(s) URL")
    domain = primary_domain_for_website(website)
    if domain is None:
        raise ValueError("website must contain a valid primary domain")
    return domain


def next_available_slug(
    connection: Connection,
    *,
    requested_slug: str | None,
    company_name: str,
    domain: str | None,
) -> str:
    requested = re.sub(r"[^a-z0-9]+", "-", (requested_slug or "").lower()).strip("-")
    name_slug = re.sub(r"[^a-z0-9]+", "-", normalize_company_name(company_name)).strip("-")
    base = requested or name_slug or re.sub(r"[^a-z0-9]+", "-", domain or "").strip("-")
    base = base or "company"
    candidates = [base]
    if domain:
        candidates.append(f"{base}-{domain.split('.')[0]}")
    for candidate in candidates:
        if connection.scalar(
            select(companies_table.c.id).where(companies_table.c.slug == candidate)
        ) is None:
            return candidate
    suffix = 2
    while True:
        candidate = f"{base}-{suffix}"
        if connection.scalar(
            select(companies_table.c.id).where(companies_table.c.slug == candidate)
        ) is None:
            return candidate
        suffix += 1


def _yc_source_slug(source: dict[str, Any]) -> str:
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    raw_payload = (
        metadata.get("raw_payload") if isinstance(metadata.get("raw_payload"), dict) else {}
    )
    slug = str(metadata.get("slug") or raw_payload.get("slug") or "").strip().lower()
    if not slug:
        raise ValueError(f"YC company source {source.get('external_id')!r} has no slug")
    return slug


def _resolve_yc_source(
    payload: dict[str, Any],
    *,
    sources_by_external_id: dict[str, dict[str, Any]],
    sources_by_slug: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    external_company_id = _optional_text(
        payload.get("company_id") or payload.get("companyId")
    )
    if external_company_id and external_company_id in sources_by_external_id:
        return sources_by_external_id[external_company_id]
    slug = _optional_text(payload.get("company_slug") or payload.get("companySlug"))
    if slug and slug.lower() in sources_by_slug:
        return sources_by_slug[slug.lower()]
    raise ValueError(
        f"YC job {payload.get('id') or payload.get('objectID')!r} has no registered company source"
    )


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
    if not isinstance(value, list):
        return []
    return [result for item in value if (result := _optional_text(item))]


def _optional_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)
