from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable

from yc_radar.core.config import get_settings
from yc_radar.domain.models import Company
from yc_radar.services.database import engine_from_url, fetch_company_row, fetch_company_rows


def _company_from_db_row(row: dict[str, Any]) -> Company:
    return Company(
        id=row.get("id"),
        name=row.get("name", ""),
        slug=row.get("slug", ""),
        yc_url=row.get("yc_url") or f"https://www.ycombinator.com/companies/{row.get('slug', '')}",
        website=row.get("website"),
        one_liner=row.get("one_liner"),
        batch=row.get("batch"),
        status=row.get("status"),
        stage=row.get("stage"),
        team_size=row.get("team_size"),
        isHiring=bool(row.get("is_hiring")),
        all_locations=row.get("all_locations"),
        regions=list(row.get("regions") or []),
        industry=row.get("industry"),
        subindustry=row.get("subindustry"),
        industries=list(row.get("industries") or []),
        tags=list(row.get("tags") or []),
        prototype_score=row.get("prototype_score"),
        prototype_angle=row.get("prototype_angle"),
    )


class CompanyRepository:
    def __init__(self, database_url: str | None = None) -> None:
        settings = get_settings()
        self.engine = engine_from_url(database_url or settings.database_url)

    def list(self) -> list[Company]:
        return [_company_from_db_row(row) for row in fetch_company_rows(self.engine)]

    def get_by_slug(self, slug: str) -> Company | None:
        row = fetch_company_row(self.engine, slug.lower())
        return _company_from_db_row(row) if row else None

    def search(
        self,
        query: str | None = None,
        hiring: bool | None = None,
        remote: bool | None = None,
        max_team_size: int | None = None,
        industries: Iterable[str] | None = None,
    ) -> list[Company]:
        companies = self.list()
        terms = [term.lower() for term in industries or []]

        if query:
            query_lower = query.lower()
            companies = [
                company
                for company in companies
                if query_lower in company.text_blob
                or query_lower in (company.website or "").lower()
                or query_lower in (company.yc_url or "").lower()
            ]
        if hiring is not None:
            companies = [company for company in companies if company.is_hiring == hiring]
        if remote is not None:
            companies = [company for company in companies if company.is_remote_friendly == remote]
        if max_team_size is not None:
            companies = [
                company
                for company in companies
                if company.team_size is not None and company.team_size <= max_team_size
            ]
        if terms:
            companies = [
                company for company in companies if any(term in company.text_blob for term in terms)
            ]

        return sorted(companies, key=lambda company: company.prototype_score or 0, reverse=True)


@lru_cache
def get_company_repository() -> CompanyRepository:
    return CompanyRepository()
