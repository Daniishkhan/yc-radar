from __future__ import annotations

from typing import Any

from yc_radar.core.config import get_settings
from yc_radar.domain.models import Company
from yc_radar.services.database import engine_from_url, fetch_company_rows


def _company_from_db_row(row: dict[str, Any]) -> Company:
    return Company(
        id=row.get("id"),
        name=row.get("name", ""),
        slug=row.get("slug", ""),
        yc_url=row.get("yc_url"),
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
