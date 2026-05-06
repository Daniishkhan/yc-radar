from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from yc_radar.core.config import get_settings

metadata = MetaData()
BATCH_SIZE = 30

companies_table = Table(
    "companies",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String, nullable=False),
    Column("slug", String, nullable=False, unique=True, index=True),
    Column("yc_url", String, nullable=False),
    Column("website", String),
    Column("one_liner", Text),
    Column("batch", String),
    Column("status", String),
    Column("stage", String),
    Column("team_size", Integer),
    Column("is_hiring", Boolean, nullable=False, default=False),
    Column("all_locations", Text),
    Column("regions", JSON, nullable=False, default=list),
    Column("industry", String),
    Column("subindustry", String),
    Column("industries", JSON, nullable=False, default=list),
    Column("tags", JSON, nullable=False, default=list),
    Column("prototype_score", Integer),
    Column("prototype_angle", Text),
    Column("raw_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

yc_job_postings_table = Table(
    "yc_job_postings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("company_name", String, nullable=False),
    Column("company_yc_url", String, nullable=False),
    Column("title", String, nullable=False),
    Column("url", String, nullable=False),
    Column("absolute_url", String, nullable=False),
    Column("apply_url", Text),
    Column("location", Text),
    Column("type", String),
    Column("role", String),
    Column("role_specific_type", String),
    Column("pretty_role", String),
    Column("salary_range", String),
    Column("equity_range", String),
    Column("min_experience", String),
    Column("min_school_year", String),
    Column("visa", String),
    Column("skills", JSON, nullable=False, default=list),
    Column("is_incomplete", Boolean, nullable=False, default=False),
    Column("created_at_text", String),
    Column("last_active_text", String),
    Column("raw_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

career_surfaces_table = Table(
    "career_surfaces",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("company_name", String, nullable=False),
    Column("website", String),
    Column("yc_is_hiring", Boolean, nullable=False, default=False),
    Column("yc_job_count", Integer, nullable=False, default=0),
    Column("url", Text, nullable=False),
    Column("normalized_url", Text, nullable=False),
    Column("url_type", String, nullable=False),
    Column("source", String, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("http_status", Integer),
    Column("evidence", Text),
    Column("checked_at", DateTime(timezone=True), nullable=False),
    Column("raw_json", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("company_slug", "normalized_url", name="uq_career_surface_company_url"),
)


def engine_from_url(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    if url.startswith("sqlite:///"):
        db_path = Path(url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, future=True)


def create_schema(engine: Engine) -> None:
    metadata.create_all(engine)


def has_companies(engine: Engine) -> bool:
    create_schema(engine)
    with engine.connect() as connection:
        return bool(connection.scalar(select(func.count()).select_from(companies_table)))


def upsert_companies(engine: Engine, companies: list[dict[str, Any]]) -> None:
    create_schema(engine)
    if not companies:
        return
    rows = [_company_row(company) for company in companies]
    _upsert_rows(engine, companies_table, rows, index_elements=["id"])


def upsert_yc_job_postings(engine: Engine, jobs: list[dict[str, Any]]) -> None:
    create_schema(engine)
    if not jobs:
        return
    rows = [_job_row(job) for job in jobs]
    _upsert_rows(engine, yc_job_postings_table, rows, index_elements=["id"])


def replace_career_surfaces(
    engine: Engine,
    surfaces: list[dict[str, Any]],
    *,
    company_slugs: list[str] | None = None,
) -> None:
    create_schema(engine)
    with engine.begin() as connection:
        if company_slugs:
            connection.execute(
                delete(career_surfaces_table).where(
                    career_surfaces_table.c.company_slug.in_(company_slugs)
                )
            )
        else:
            connection.execute(delete(career_surfaces_table))
        if surfaces:
            rows = [_career_surface_row(surface) for surface in surfaces]
            for chunk in _chunks(rows, BATCH_SIZE):
                statement = sqlite_insert(career_surfaces_table).values(chunk)
                update_columns = {
                    column.name: getattr(statement.excluded, column.name)
                    for column in career_surfaces_table.columns
                    if column.name not in {"id", "created_at"}
                }
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["company_slug", "normalized_url"],
                        set_=update_columns,
                    )
                )


def fetch_company_rows(engine: Engine) -> list[dict[str, Any]]:
    create_schema(engine)
    with engine.connect() as connection:
        rows = connection.execute(select(companies_table)).mappings().all()
    return [dict(row) for row in rows]


def fetch_company_row(engine: Engine, slug: str) -> dict[str, Any] | None:
    create_schema(engine)
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(companies_table).where(companies_table.c.slug == slug.lower())
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def fetch_companies_for_discovery(engine: Engine, *, limit: int | None = None) -> list[dict[str, Any]]:
    create_schema(engine)
    statement = select(companies_table).order_by(companies_table.c.slug)
    if limit is not None:
        statement = statement.limit(limit)
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    return [dict(row) for row in rows]


def fetch_yc_job_rows(engine: Engine) -> list[dict[str, Any]]:
    create_schema(engine)
    with engine.connect() as connection:
        rows = connection.execute(select(yc_job_postings_table)).mappings().all()
    return [dict(row) for row in rows]


def _upsert_rows(
    engine: Engine,
    table: Table,
    rows: list[dict[str, Any]],
    *,
    index_elements: list[str],
) -> None:
    with engine.begin() as connection:
        for chunk in _chunks(rows, BATCH_SIZE):
            statement = sqlite_insert(table).values(chunk)
            update_columns = {
                column.name: getattr(statement.excluded, column.name)
                for column in table.columns
                if column.name not in {"created_at"}
            }
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=index_elements,
                    set_=update_columns,
                )
            )


def _company_row(company: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": _to_int(company.get("id")) or _to_int(company.get("objectID")),
        "name": company.get("name") or "",
        "slug": str(company.get("slug") or "").lower(),
        "yc_url": f"https://www.ycombinator.com/companies/{company.get('slug', '')}",
        "website": company.get("website"),
        "one_liner": company.get("one_liner"),
        "batch": company.get("batch"),
        "status": company.get("status"),
        "stage": company.get("stage"),
        "team_size": _to_int(company.get("team_size")),
        "is_hiring": bool(company.get("isHiring")),
        "all_locations": company.get("all_locations"),
        "regions": _as_list(company.get("regions")),
        "industry": company.get("industry"),
        "subindustry": company.get("subindustry"),
        "industries": _as_list(company.get("industries")),
        "tags": _as_list(company.get("tags")),
        "prototype_score": _to_int(company.get("prototype_score")),
        "prototype_angle": company.get("prototype_angle"),
        "raw_json": _json_safe(company),
        "created_at": now,
        "updated_at": now,
    }


def _job_row(job: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    relative_url = job.get("url") or ""
    return {
        "id": _to_int(job.get("id")),
        "company_id": _to_int(job.get("company_id")),
        "company_slug": str(job.get("company_slug") or "").lower(),
        "company_name": job.get("company_name") or job.get("companyName") or "",
        "company_yc_url": job.get("company_yc_url")
        or urljoin("https://www.ycombinator.com", job.get("companyUrl") or ""),
        "title": job.get("title") or "",
        "url": relative_url,
        "absolute_url": urljoin("https://www.ycombinator.com", relative_url),
        "apply_url": job.get("applyUrl"),
        "location": job.get("location"),
        "type": job.get("type"),
        "role": job.get("role"),
        "role_specific_type": job.get("roleSpecificType"),
        "pretty_role": job.get("prettyRole"),
        "salary_range": job.get("salaryRange"),
        "equity_range": job.get("equityRange"),
        "min_experience": job.get("minExperience"),
        "min_school_year": job.get("minSchoolYear"),
        "visa": job.get("visa"),
        "skills": _as_list(job.get("skills")),
        "is_incomplete": bool(job.get("isIncomplete")),
        "created_at_text": job.get("createdAt"),
        "last_active_text": job.get("lastActive"),
        "raw_json": _json_safe(job),
        "created_at": now,
        "updated_at": now,
    }


def _career_surface_row(surface: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    checked_at = _to_datetime(surface.get("checked_at")) or now
    return {
        "company_id": _to_int(surface.get("company_id")),
        "company_slug": str(surface.get("company_slug") or "").lower(),
        "company_name": surface.get("company_name") or "",
        "website": surface.get("website"),
        "yc_is_hiring": bool(surface.get("yc_is_hiring")),
        "yc_job_count": _to_int(surface.get("yc_job_count")) or 0,
        "url": surface.get("url") or surface.get("normalized_url") or "",
        "normalized_url": surface.get("normalized_url") or surface.get("url") or "",
        "url_type": surface.get("url_type") or "unknown",
        "source": surface.get("source") or "unknown",
        "confidence": float(surface.get("confidence") or 0),
        "http_status": _to_int(surface.get("http_status")),
        "evidence": surface.get("evidence"),
        "checked_at": checked_at,
        "raw_json": _json_safe(surface),
        "created_at": now,
        "updated_at": now,
    }


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part.strip() for part in value.split(";") if part.strip()]
    return [value]


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _chunks(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]
