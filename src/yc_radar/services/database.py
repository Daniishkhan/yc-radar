from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, insert as pg_insert
from sqlalchemy.engine import Engine, make_url

from yc_radar.core.config import get_settings

metadata = MetaData()
BATCH_SIZE = 100
EMBEDDING_DIMENSIONS = 1536

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
    Column("regions", JSONB, nullable=False, default=list),
    Column("industry", String),
    Column("subindustry", String),
    Column("industries", JSONB, nullable=False, default=list),
    Column("tags", JSONB, nullable=False, default=list),
    Column("prototype_score", Integer),
    Column("prototype_angle", Text),
    Column("raw_json", JSONB, nullable=False),
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
    Column("skills", JSONB, nullable=False, default=list),
    Column("is_incomplete", Boolean, nullable=False, default=False),
    Column("created_at_text", String),
    Column("last_active_text", String),
    Column("raw_json", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

career_page_discovery_events_table = Table(
    "career_page_discovery_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("company_name", String, nullable=False),
    Column("website", String),
    Column("yc_is_hiring", Boolean, nullable=False, default=False),
    Column("yc_job_count", Integer, nullable=False, default=0),
    Column("url", Text, nullable=False),
    Column("normalized_url", Text, nullable=False),
    Column("page_type", String, nullable=False),
    Column("discovery_source", String, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("http_status", Integer),
    Column("evidence", Text),
    Column("checked_at", DateTime(timezone=True), nullable=False),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

company_career_pages_table = Table(
    "company_career_pages",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("company_name", String, nullable=False),
    Column("website", String),
    Column("yc_is_hiring", Boolean, nullable=False, default=False),
    Column("yc_job_count", Integer, nullable=False, default=0),
    Column("career_page_url", Text, nullable=False),
    Column("normalized_url", Text, nullable=False),
    Column("page_type", String, nullable=False),
    Column("discovery_source", String, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("http_status", Integer),
    Column("evidence", Text),
    Column("is_primary", Boolean, nullable=False, default=False),
    Column("observed_source_count", Integer, nullable=False, default=1),
    Column("checked_at", DateTime(timezone=True), nullable=False),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("company_slug", "normalized_url", name="uq_company_career_page_url"),
)

discovered_urls_table = Table(
    "discovered_urls",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("company_name", String, nullable=False),
    Column("website", String),
    Column("url", Text, nullable=False),
    Column("normalized_url", Text, nullable=False),
    Column("url_key", String, nullable=False),
    Column("url_kind", String, nullable=False, index=True),
    Column("discovery_sources", JSONB, nullable=False, default=list),
    Column("evidence_samples", JSONB, nullable=False, default=list),
    Column("source_event_count", Integer, nullable=False, default=1),
    Column("confidence", Float, nullable=False, default=0.0),
    Column("fetch_priority", Float, nullable=False, default=0.0),
    Column("http_status", Integer),
    Column("is_primary", Boolean, nullable=False, default=False),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("company_slug", "url_key", name="uq_discovered_url_company_key"),
)

career_page_discovery_statuses_table = Table(
    "career_page_discovery_statuses",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, unique=True, index=True),
    Column("company_name", String, nullable=False),
    Column("website", String),
    Column("status", String, nullable=False, index=True),
    Column("discovery_event_count", Integer, nullable=False, default=0),
    Column("career_page_count", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("checked_at", DateTime(timezone=True), nullable=False),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

source_documents_table = Table(
    "source_documents",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("discovered_url_id", BigInteger, ForeignKey("discovered_urls.id", ondelete="SET NULL")),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("company_name", String, nullable=False),
    Column("source_type", String, nullable=False, index=True),
    Column("source_key", String, nullable=False),
    Column("url", Text),
    Column("normalized_url", Text),
    Column("title", Text),
    Column("raw_text", Text),
    Column("clean_text", Text),
    Column("content_hash", String, nullable=False, index=True),
    Column("http_status", Integer),
    Column("fetched_at", DateTime(timezone=True)),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column(
        "search_vector",
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(clean_text, ''))",
            persisted=True,
        ),
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("source_type", "source_key", name="uq_source_document_source_key"),
)

page_classifications_table = Table(
    "page_classifications",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("source_document_id", BigInteger, ForeignKey("source_documents.id", ondelete="CASCADE")),
    Column("discovered_url_id", BigInteger, ForeignKey("discovered_urls.id", ondelete="SET NULL")),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("company_name", String, nullable=False),
    Column("url", Text, nullable=False),
    Column("normalized_url", Text, nullable=False),
    Column("page_kind", String, nullable=False, index=True),
    Column("confidence", Float, nullable=False, default=0.0),
    Column("parser_name", String, nullable=False),
    Column("parser_version", String, nullable=False),
    Column("http_status", Integer),
    Column("job_title", Text),
    Column("role_titles", JSONB, nullable=False, default=list),
    Column("job_count", Integer, nullable=False, default=0),
    Column("evidence", JSONB, nullable=False, default=dict),
    Column("classified_at", DateTime(timezone=True), nullable=False),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "source_document_id",
        "parser_name",
        name="uq_page_classification_document_parser",
    ),
)

external_job_postings_table = Table(
    "external_job_postings",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("company_name", String, nullable=False),
    Column(
        "source_document_id", BigInteger, ForeignKey("source_documents.id", ondelete="SET NULL")
    ),
    Column("source", String, nullable=False, index=True),
    Column("source_job_id", String),
    Column("posting_url", Text, nullable=False),
    Column("normalized_url", Text, nullable=False),
    Column("apply_url", Text),
    Column("title", Text, nullable=False),
    Column("description_text", Text),
    Column("location", Text),
    Column("employment_type", String),
    Column("department", String),
    Column("seniority", String),
    Column("salary_range", Text),
    Column("equity_range", Text),
    Column("visa", Text),
    Column("remote_policy", Text),
    Column("status", String, nullable=False, default="active"),
    Column("role_fit", String, nullable=False, default="unknown"),
    Column("extraction_confidence", Float, nullable=False, default=0.0),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("source", "normalized_url", name="uq_external_job_source_url"),
)

job_extraction_runs_table = Table(
    "job_extraction_runs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("source_document_id", BigInteger, ForeignKey("source_documents.id", ondelete="CASCADE")),
    Column("parser_name", String, nullable=False),
    Column("model", String),
    Column("prompt_version", String),
    Column("status", String, nullable=False),
    Column("extracted_jobs_count", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
)

document_chunks_table = Table(
    "document_chunks",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("source_document_id", BigInteger, ForeignKey("source_documents.id", ondelete="CASCADE")),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("source_type", String, nullable=False, index=True),
    Column("chunk_index", Integer, nullable=False),
    Column("chunk_text", Text, nullable=False),
    Column("content_hash", String, nullable=False, index=True),
    Column("token_count", Integer),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column(
        "search_vector",
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(chunk_text, ''))",
            persisted=True,
        ),
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "source_document_id",
        "chunk_index",
        name="uq_document_chunk_source_index",
    ),
)

document_embeddings_table = Table(
    "document_embeddings",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("chunk_id", BigInteger, ForeignKey("document_chunks.id", ondelete="CASCADE")),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("embedding_model", String, nullable=False, index=True),
    Column("embedding_dimensions", Integer, nullable=False, default=EMBEDDING_DIMENSIONS),
    Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
    Column("embedded_at", DateTime(timezone=True), nullable=False),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "chunk_id",
        "embedding_model",
        "embedding_dimensions",
        name="uq_document_embedding_chunk_model",
    ),
)

job_role_signals_table = Table(
    "job_role_signals",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("company_id", Integer, index=True),
    Column("company_slug", String, nullable=False, index=True),
    Column("yc_job_id", Integer, ForeignKey("yc_job_postings.id", ondelete="CASCADE")),
    Column(
        "external_job_id", BigInteger, ForeignKey("external_job_postings.id", ondelete="CASCADE")
    ),
    Column("signal_type", String, nullable=False, index=True),
    Column("signal_value", String, nullable=False, index=True),
    Column("confidence", Float, nullable=False, default=0.0),
    Column("evidence", Text),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

Index(
    "ix_source_documents_search_vector",
    source_documents_table.c.search_vector,
    postgresql_using="gin",
)
Index(
    "ix_document_chunks_search_vector",
    document_chunks_table.c.search_vector,
    postgresql_using="gin",
)
Index(
    "ix_document_embeddings_embedding_hnsw",
    document_embeddings_table.c.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
)
Index("ix_discovered_urls_priority", discovered_urls_table.c.fetch_priority)
Index("ix_external_job_postings_role_fit", external_job_postings_table.c.role_fit)


def engine_from_url(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("YC Radar is Postgres-only. Set DATABASE_URL to a postgresql+psycopg URL.")
    return create_engine(parsed, future=True, pool_pre_ping=True)


def create_schema(engine: Engine, *, checkfirst: bool = True) -> None:
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
    metadata.create_all(engine, checkfirst=checkfirst)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE OR REPLACE VIEW company_primary_career_pages AS
                SELECT
                    company_id,
                    company_slug,
                    company_name,
                    website,
                    yc_is_hiring,
                    yc_job_count,
                    career_page_url,
                    page_type,
                    discovery_source,
                    confidence,
                    http_status,
                    evidence,
                    checked_at
                FROM company_career_pages
                WHERE is_primary = true
                """
            )
        )


def rebuild_database(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW IF EXISTS company_primary_career_pages"))
        for table in reversed(metadata.sorted_tables):
            connection.execute(text(f'DROP TABLE IF EXISTS "{table.name}" CASCADE'))
    create_schema(engine, checkfirst=False)


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


def upsert_source_documents(engine: Engine, documents: list[dict[str, Any]]) -> None:
    create_schema(engine)
    if not documents:
        return
    rows = [_source_document_row(document) for document in documents]
    _upsert_rows(
        engine,
        source_documents_table,
        rows,
        index_elements=["source_type", "source_key"],
    )


def upsert_page_classifications(engine: Engine, classifications: list[dict[str, Any]]) -> None:
    create_schema(engine)
    if not classifications:
        return
    rows = [_page_classification_row(classification) for classification in classifications]
    _upsert_rows(
        engine,
        page_classifications_table,
        rows,
        index_elements=["source_document_id", "parser_name"],
    )


def upsert_external_job_postings(engine: Engine, jobs: list[dict[str, Any]]) -> None:
    create_schema(engine)
    if not jobs:
        return
    rows = [_external_job_row(job) for job in jobs]
    _upsert_rows(
        engine,
        external_job_postings_table,
        rows,
        index_elements=["source", "normalized_url"],
    )


def upsert_career_page_discovery_statuses(
    engine: Engine,
    statuses: list[dict[str, Any]],
) -> None:
    create_schema(engine)
    if not statuses:
        return
    rows = [_career_page_discovery_status_row(status) for status in statuses]
    _upsert_rows(
        engine,
        career_page_discovery_statuses_table,
        rows,
        index_elements=["company_slug"],
    )


def replace_career_page_data(
    engine: Engine,
    discovery_events: list[dict[str, Any]],
    career_pages: list[dict[str, Any]],
    *,
    company_slugs: list[str] | None = None,
) -> None:
    create_schema(engine)
    with engine.begin() as connection:
        if company_slugs:
            connection.execute(
                delete(career_page_discovery_events_table).where(
                    career_page_discovery_events_table.c.company_slug.in_(company_slugs)
                )
            )
            connection.execute(
                delete(company_career_pages_table).where(
                    company_career_pages_table.c.company_slug.in_(company_slugs)
                )
            )
            connection.execute(
                delete(discovered_urls_table).where(
                    discovered_urls_table.c.company_slug.in_(company_slugs)
                )
            )
            connection.execute(
                delete(career_page_discovery_statuses_table).where(
                    career_page_discovery_statuses_table.c.company_slug.in_(company_slugs)
                )
            )
        else:
            connection.execute(delete(career_page_discovery_events_table))
            connection.execute(delete(company_career_pages_table))
            connection.execute(delete(discovered_urls_table))
            connection.execute(delete(career_page_discovery_statuses_table))
        if discovery_events:
            rows = [_career_page_discovery_event_row(event) for event in discovery_events]
            for chunk in _chunks(rows, BATCH_SIZE):
                connection.execute(career_page_discovery_events_table.insert(), chunk)
        if career_pages:
            rows = [_company_career_page_row(page) for page in career_pages]
            for chunk in _chunks(rows, BATCH_SIZE):
                statement = pg_insert(company_career_pages_table).values(chunk)
                update_columns = _upsert_update_columns(statement, company_career_pages_table)
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["company_slug", "normalized_url"],
                        set_=update_columns,
                    )
                )
            discovered_rows = [_discovered_url_row_from_career_page(page) for page in career_pages]
            for chunk in _chunks(discovered_rows, BATCH_SIZE):
                statement = pg_insert(discovered_urls_table).values(chunk)
                update_columns = _upsert_update_columns(statement, discovered_urls_table)
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["company_slug", "url_key"],
                        set_=update_columns,
                    )
                )


def drop_legacy_career_surfaces_table(engine: Engine) -> None:
    create_schema(engine)
    if "career_surfaces" not in inspect(engine).get_table_names():
        return
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS career_surfaces"))


def truncate_database(engine: Engine) -> None:
    create_schema(engine)
    table_names = ", ".join(f'"{table.name}"' for table in reversed(metadata.sorted_tables))
    if not table_names:
        return
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))


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


def fetch_companies_for_discovery(
    engine: Engine, *, limit: int | None = None
) -> list[dict[str, Any]]:
    create_schema(engine)
    statement = select(companies_table).order_by(companies_table.c.slug)
    if limit is not None:
        statement = statement.limit(limit)
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    return [dict(row) for row in rows]


def fetch_career_page_discovery_event_rows(
    engine: Engine,
    *,
    company_slugs: list[str] | None = None,
) -> list[dict[str, Any]]:
    create_schema(engine)
    statement = select(career_page_discovery_events_table).order_by(
        career_page_discovery_events_table.c.company_slug,
        career_page_discovery_events_table.c.confidence.desc(),
        career_page_discovery_events_table.c.normalized_url,
    )
    if company_slugs:
        statement = statement.where(
            career_page_discovery_events_table.c.company_slug.in_(company_slugs)
        )
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    return [dict(row) for row in rows]


def fetch_company_career_page_rows(
    engine: Engine,
    *,
    company_slugs: list[str] | None = None,
) -> list[dict[str, Any]]:
    create_schema(engine)
    statement = select(company_career_pages_table).order_by(
        company_career_pages_table.c.company_slug,
        company_career_pages_table.c.confidence.desc(),
        company_career_pages_table.c.normalized_url,
    )
    if company_slugs:
        statement = statement.where(company_career_pages_table.c.company_slug.in_(company_slugs))
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    return [dict(row) for row in rows]


def fetch_discovered_url_rows(
    engine: Engine,
    *,
    company_slugs: list[str] | None = None,
    limit: int | None = None,
    only_unclassified: bool = False,
) -> list[dict[str, Any]]:
    create_schema(engine)
    statement = select(discovered_urls_table).where(discovered_urls_table.c.is_active.is_(True))
    if company_slugs:
        statement = statement.where(discovered_urls_table.c.company_slug.in_(company_slugs))
    if only_unclassified:
        classified = select(page_classifications_table.c.id).where(
            page_classifications_table.c.discovered_url_id == discovered_urls_table.c.id
        )
        statement = statement.where(~classified.exists())
    statement = statement.order_by(
        discovered_urls_table.c.fetch_priority.desc(),
        discovered_urls_table.c.confidence.desc(),
        discovered_urls_table.c.company_slug,
        discovered_urls_table.c.normalized_url,
    )
    if limit is not None:
        statement = statement.limit(limit)
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    return [dict(row) for row in rows]


def fetch_source_document_rows(
    engine: Engine,
    *,
    source_type: str | None = None,
    source_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    create_schema(engine)
    statement = select(source_documents_table)
    if source_type:
        statement = statement.where(source_documents_table.c.source_type == source_type)
    if source_keys:
        statement = statement.where(source_documents_table.c.source_key.in_(source_keys))
    statement = statement.order_by(source_documents_table.c.company_slug, source_documents_table.c.id)
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    return [dict(row) for row in rows]


def fetch_page_classification_rows(
    engine: Engine,
    *,
    company_slugs: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    create_schema(engine)
    statement = select(page_classifications_table).order_by(
        page_classifications_table.c.classified_at.desc(),
        page_classifications_table.c.company_slug,
        page_classifications_table.c.url,
    )
    if company_slugs:
        statement = statement.where(page_classifications_table.c.company_slug.in_(company_slugs))
    if limit is not None:
        statement = statement.limit(limit)
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    return [dict(row) for row in rows]


def fetch_completed_career_discovery_slugs(
    engine: Engine,
    *,
    company_slugs: list[str] | None = None,
) -> set[str]:
    create_schema(engine)
    statement = select(career_page_discovery_statuses_table.c.company_slug).where(
        career_page_discovery_statuses_table.c.status == "completed"
    )
    if company_slugs:
        statement = statement.where(
            career_page_discovery_statuses_table.c.company_slug.in_(company_slugs)
        )
    with engine.connect() as connection:
        return set(connection.scalars(statement).all())


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
            statement = pg_insert(table).values(chunk)
            update_columns = _upsert_update_columns(statement, table)
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=index_elements,
                    set_=update_columns,
                )
            )


def _upsert_update_columns(statement: Any, table: Table) -> dict[str, Any]:
    return {
        column.name: getattr(statement.excluded, column.name)
        for column in table.columns
        if column.name not in {"id", "created_at"} and column.computed is None
    }


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


def _career_page_discovery_event_row(event: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    checked_at = _to_datetime(event.get("checked_at")) or now
    return {
        "company_id": _to_int(event.get("company_id")),
        "company_slug": str(event.get("company_slug") or "").lower(),
        "company_name": event.get("company_name") or "",
        "website": event.get("website"),
        "yc_is_hiring": bool(event.get("yc_is_hiring")),
        "yc_job_count": _to_int(event.get("yc_job_count")) or 0,
        "url": event.get("url") or event.get("normalized_url") or "",
        "normalized_url": event.get("normalized_url") or event.get("url") or "",
        "page_type": event.get("page_type") or "unknown",
        "discovery_source": event.get("discovery_source") or "unknown",
        "confidence": float(event.get("confidence") or 0),
        "http_status": _to_int(event.get("http_status")),
        "evidence": event.get("evidence"),
        "checked_at": checked_at,
        "raw_json": _json_safe(event),
        "created_at": now,
        "updated_at": now,
    }


def _company_career_page_row(page: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    checked_at = _to_datetime(page.get("checked_at")) or now
    return {
        "company_id": _to_int(page.get("company_id")),
        "company_slug": str(page.get("company_slug") or "").lower(),
        "company_name": page.get("company_name") or "",
        "website": page.get("website"),
        "yc_is_hiring": bool(page.get("yc_is_hiring")),
        "yc_job_count": _to_int(page.get("yc_job_count")) or 0,
        "career_page_url": page.get("career_page_url") or page.get("url") or "",
        "normalized_url": page.get("normalized_url") or page.get("career_page_url") or "",
        "page_type": page.get("page_type") or "unknown",
        "discovery_source": page.get("discovery_source") or "unknown",
        "confidence": float(page.get("confidence") or 0),
        "http_status": _to_int(page.get("http_status")),
        "evidence": page.get("evidence"),
        "is_primary": bool(page.get("is_primary")),
        "observed_source_count": _to_int(page.get("observed_source_count")) or 1,
        "checked_at": checked_at,
        "raw_json": _json_safe(page),
        "created_at": now,
        "updated_at": now,
    }


def _discovered_url_row_from_career_page(page: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    checked_at = _to_datetime(page.get("checked_at")) or now
    normalized_url = page.get("normalized_url") or page.get("career_page_url") or ""
    raw_json = _json_safe(page.get("raw_json") or {})
    discovery_sources = raw_json.get("discovery_sources")
    if not isinstance(discovery_sources, list) or not discovery_sources:
        discovery_sources = [page.get("discovery_source") or "unknown"]
    evidence = page.get("evidence")
    evidence_samples = [evidence] if evidence else []
    return {
        "company_id": _to_int(page.get("company_id")),
        "company_slug": str(page.get("company_slug") or "").lower(),
        "company_name": page.get("company_name") or "",
        "website": page.get("website"),
        "url": page.get("career_page_url") or normalized_url,
        "normalized_url": normalized_url,
        "url_key": _url_dedupe_key(normalized_url),
        "url_kind": page.get("page_type") or "unknown",
        "discovery_sources": _as_list(discovery_sources),
        "evidence_samples": evidence_samples,
        "source_event_count": _to_int(raw_json.get("event_count"))
        or _to_int(page.get("observed_source_count"))
        or 1,
        "confidence": float(page.get("confidence") or 0),
        "fetch_priority": (1.0 if page.get("is_primary") else 0.0)
        + float(page.get("confidence") or 0),
        "http_status": _to_int(page.get("http_status")),
        "is_primary": bool(page.get("is_primary")),
        "is_active": True,
        "first_seen_at": checked_at,
        "last_seen_at": checked_at,
        "raw_json": _json_safe(page),
        "created_at": now,
        "updated_at": now,
    }


def _career_page_discovery_status_row(status: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    checked_at = _to_datetime(status.get("checked_at")) or now
    return {
        "company_id": _to_int(status.get("company_id")),
        "company_slug": str(status.get("company_slug") or "").lower(),
        "company_name": status.get("company_name") or "",
        "website": status.get("website"),
        "status": status.get("status") or "completed",
        "discovery_event_count": _to_int(status.get("discovery_event_count")) or 0,
        "career_page_count": _to_int(status.get("career_page_count")) or 0,
        "error": status.get("error"),
        "checked_at": checked_at,
        "raw_json": _json_safe(status),
        "created_at": now,
        "updated_at": now,
    }


def _source_document_row(document: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    observed_at = _to_datetime(document.get("observed_at")) or now
    return {
        "discovered_url_id": _to_int(document.get("discovered_url_id")),
        "company_id": _to_int(document.get("company_id")),
        "company_slug": str(document.get("company_slug") or "").lower(),
        "company_name": document.get("company_name") or "",
        "source_type": document.get("source_type") or "unknown",
        "source_key": document.get("source_key") or document.get("content_hash") or "",
        "url": document.get("url"),
        "normalized_url": document.get("normalized_url") or document.get("url"),
        "title": document.get("title"),
        "raw_text": document.get("raw_text"),
        "clean_text": document.get("clean_text"),
        "content_hash": document.get("content_hash") or "",
        "http_status": _to_int(document.get("http_status")),
        "fetched_at": _to_datetime(document.get("fetched_at")),
        "observed_at": observed_at,
        "raw_json": _json_safe(document),
        "created_at": now,
        "updated_at": now,
    }


def _page_classification_row(classification: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    classified_at = _to_datetime(classification.get("classified_at")) or now
    return {
        "source_document_id": _to_int(classification.get("source_document_id")),
        "discovered_url_id": _to_int(classification.get("discovered_url_id")),
        "company_id": _to_int(classification.get("company_id")),
        "company_slug": str(classification.get("company_slug") or "").lower(),
        "company_name": classification.get("company_name") or "",
        "url": classification.get("url") or classification.get("normalized_url") or "",
        "normalized_url": classification.get("normalized_url") or classification.get("url") or "",
        "page_kind": classification.get("page_kind") or "unknown",
        "confidence": float(classification.get("confidence") or 0),
        "parser_name": classification.get("parser_name") or "unknown",
        "parser_version": classification.get("parser_version") or "unknown",
        "http_status": _to_int(classification.get("http_status")),
        "job_title": classification.get("job_title"),
        "role_titles": _as_list(classification.get("role_titles")),
        "job_count": _to_int(classification.get("job_count")) or 0,
        "evidence": _json_safe(classification.get("evidence") or {}),
        "classified_at": classified_at,
        "raw_json": _json_safe(classification),
        "created_at": now,
        "updated_at": now,
    }


def _external_job_row(job: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    observed_at = _to_datetime(job.get("observed_at")) or now
    return {
        "company_id": _to_int(job.get("company_id")),
        "company_slug": str(job.get("company_slug") or "").lower(),
        "company_name": job.get("company_name") or "",
        "source_document_id": _to_int(job.get("source_document_id")),
        "source": job.get("source") or "unknown",
        "source_job_id": job.get("source_job_id"),
        "posting_url": job.get("posting_url") or job.get("url") or "",
        "normalized_url": job.get("normalized_url")
        or job.get("posting_url")
        or job.get("url")
        or "",
        "apply_url": job.get("apply_url"),
        "title": job.get("title") or "",
        "description_text": job.get("description_text"),
        "location": job.get("location"),
        "employment_type": job.get("employment_type"),
        "department": job.get("department"),
        "seniority": job.get("seniority"),
        "salary_range": job.get("salary_range"),
        "equity_range": job.get("equity_range"),
        "visa": job.get("visa"),
        "remote_policy": job.get("remote_policy"),
        "status": job.get("status") or "active",
        "role_fit": job.get("role_fit") or "unknown",
        "extraction_confidence": float(job.get("extraction_confidence") or 0),
        "observed_at": observed_at,
        "raw_json": _json_safe(job),
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


def _url_dedupe_key(url: str) -> str:
    parsed = urlparse(url or "")
    domain = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(("https", domain, path, "", parsed.query, ""))


def _chunks(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]
