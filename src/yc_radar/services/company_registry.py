from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from yc_radar.services.database import (
    companies_table,
    company_sources_table,
    create_schema,
    normalize_company_name,
    primary_domain_for_website,
)


class CompanyIdentityConflict(ValueError):
    """Raised when identity evidence cannot safely resolve to one neutral company."""


@dataclass(frozen=True)
class CompanyRegistrationResult:
    company_id: int
    company_created: bool
    matched_by: str


class CompanyRegistry:
    """Own neutral company identities independently from any directory or ATS provider."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

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
        domain = verified_primary_domain(website)
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
                    .values(website=website, updated_at=observed_at)
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
                        website=website,
                        primary_domain=domain,
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

    def register_source_identity(
        self,
        *,
        company_id: int,
        provider: str,
        external_company_id: str,
        source_url: str | None = None,
        raw_json: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Attach a company-directory identity; ATS board identities belong elsewhere."""
        create_schema(self.engine)
        normalized_provider = provider.strip().lower()
        normalized_external_id = external_company_id.strip()
        if not normalized_provider or not normalized_external_id:
            raise ValueError("provider and external_company_id are required")
        observed_at = now or datetime.now(UTC)
        with self.engine.begin() as connection:
            company_exists = connection.scalar(
                select(companies_table.c.id).where(companies_table.c.id == company_id)
            )
            if company_exists is None:
                raise ValueError(f"unknown company_id: {company_id}")
            existing = connection.execute(
                select(company_sources_table).where(
                    company_sources_table.c.provider == normalized_provider,
                    company_sources_table.c.external_company_id == normalized_external_id,
                )
            ).mappings().first()
            if existing is not None:
                if int(existing["company_id"]) != company_id:
                    raise CompanyIdentityConflict(
                        f"{normalized_provider} identity {normalized_external_id} belongs "
                        f"to company_id={existing['company_id']}"
                    )
                connection.execute(
                    update(company_sources_table)
                    .where(company_sources_table.c.id == existing["id"])
                    .values(
                        source_url=source_url,
                        raw_json=raw_json or existing.get("raw_json") or {},
                        last_seen_at=observed_at,
                        updated_at=observed_at,
                    )
                )
                return False
            connection.execute(
                insert(company_sources_table).values(
                    company_id=company_id,
                    provider=normalized_provider,
                    external_company_id=normalized_external_id,
                    source_url=source_url,
                    raw_json=raw_json or {},
                    first_seen_at=observed_at,
                    last_seen_at=observed_at,
                    created_at=observed_at,
                    updated_at=observed_at,
                )
            )
        return True


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
    domain: str,
) -> str:
    requested = re.sub(r"[^a-z0-9]+", "-", (requested_slug or "").lower()).strip("-")
    name_slug = re.sub(r"[^a-z0-9]+", "-", normalize_company_name(company_name)).strip("-")
    base = requested or name_slug or re.sub(r"[^a-z0-9]+", "-", domain).strip("-")
    base = base or "company"
    candidates = [base, f"{base}-{domain.split('.')[0]}"]
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
