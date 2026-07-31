from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
    cast,
    literal,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, insert as pg_insert
from sqlalchemy.engine import Engine, make_url

from yc_radar.core.config import get_settings
from yc_radar.services.url_quality import canonical_url_key

metadata = MetaData()
BATCH_SIZE = 100
EMBEDDING_DIMENSIONS = 1536
URL_INVENTORY_ADVISORY_LOCK = "yc_radar_url_cleanup_v1"
_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)
_ABSOLUTE_HTTP_URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)
_SHARED_WEBSITE_HOSTS = frozenset(
    {
        "angel.co",
        "apps.apple.com",
        "crunchbase.com",
        "facebook.com",
        "github.com",
        "googleblog.blogspot.com",
        "itunes.apple.com",
        "linkedin.com",
        "lnkd.in",
        "m.me",
        "pitchbook.com",
        "play.google.com",
        "producthunt.com",
        "substack.com",
        "ycombinator.com",
    }
)
_SHARED_ROOT_WEBSITE_BRANDS = {
    "angel.co": "angellist",
    "crunchbase.com": "crunchbase",
    "facebook.com": "facebook",
    "github.com": "github",
    "linkedin.com": "linkedin",
    "pitchbook.com": "pitchbook",
    "producthunt.com": "product hunt",
    "substack.com": "substack",
    "ycombinator.com": "y combinator",
}

companies_table = Table(
    "companies",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String, nullable=False),
    Column("normalized_name", String, nullable=False),
    Column("slug", String, nullable=False),
    Column("website", String),
    Column("primary_domain", String),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

yc_company_profiles_table = Table(
    "yc_company_profiles",
    metadata,
    Column("company_id", Integer, ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
    Column("yc_company_id", Integer, nullable=False),
    Column("yc_url", String, nullable=False),
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
    UniqueConstraint("yc_company_id", name="uq_yc_company_profiles_yc_company_id"),
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
    Column("company_slug", String, nullable=False),
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

company_sources_table = Table(
    "company_sources",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("companies.id"), nullable=False, index=True),
    Column("provider", String, nullable=False, index=True),
    Column("external_company_id", String, nullable=False),
    Column("source_url", Text),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("provider", "external_company_id", name="uq_company_source_provider_external"),
)

career_sources_table = Table(
    "career_sources",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("company_id", Integer, ForeignKey("companies.id"), nullable=False, index=True),
    Column("provider", String, nullable=False, index=True),
    Column("source_kind", String, nullable=False),
    Column("external_source_id", String, nullable=False),
    Column("source_url", Text, nullable=False),
    Column("discovered_from_url", Text),
    Column("status", String, nullable=False, default="active", index=True),
    Column("last_synced_at", DateTime(timezone=True)),
    Column("last_sync_status", String),
    Column("raw_json", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('active', 'disabled')", name="ck_career_sources_status"),
    UniqueConstraint("provider", "external_source_id", name="uq_career_source_provider_external"),
)

source_sync_runs_table = Table(
    "source_sync_runs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "career_source_id", BigInteger, ForeignKey("career_sources.id"), nullable=False, index=True
    ),
    Column("run_key", String, nullable=False),
    Column("provider", String, nullable=False),
    Column("adapter_version", String, nullable=False),
    Column("status", String, nullable=False),
    Column("is_complete_scan", Boolean, nullable=False),
    Column("http_status", Integer),
    Column("jobs_fetched", Integer, nullable=False, default=0),
    Column("jobs_added", Integer, nullable=False, default=0),
    Column("jobs_updated", Integer, nullable=False, default=0),
    Column("jobs_unchanged", Integer, nullable=False, default=0),
    Column("jobs_missed", Integer, nullable=False, default=0),
    Column("jobs_closed", Integer, nullable=False, default=0),
    Column("jobs_reactivated", Integer, nullable=False, default=0),
    Column("errors_count", Integer, nullable=False, default=0),
    Column("errors", JSONB, nullable=False, default=list),
    Column("request_metadata", JSONB, nullable=False, default=dict),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('running', 'completed', 'partial', 'failed')", name="ck_source_sync_runs_status"
    ),
    CheckConstraint("jobs_fetched >= 0", name="ck_source_sync_runs_jobs_fetched"),
    UniqueConstraint("career_source_id", "run_key", name="uq_source_sync_run_key"),
)

job_postings_table = Table(
    "job_postings",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "career_source_id", BigInteger, ForeignKey("career_sources.id"), nullable=False, index=True
    ),
    Column("company_id", Integer, ForeignKey("companies.id"), nullable=False, index=True),
    Column("provider", String, nullable=False),
    Column("external_job_id", String, nullable=False),
    Column("title", Text, nullable=False),
    Column("posting_url", Text),
    Column("apply_url", Text),
    Column("location", Text),
    Column("department", Text),
    Column("employment_type", Text),
    Column("status", String, nullable=False, default="active"),
    Column("consecutive_complete_misses", Integer, nullable=False, default=0),
    Column("content_hash", String, nullable=False, index=True),
    Column(
        "current_version_id",
        BigInteger,
        ForeignKey(
            "job_posting_versions.id", use_alter=True, name="fk_job_postings_current_version"
        ),
    ),
    Column("source_published_at", DateTime(timezone=True)),
    Column("source_updated_at", DateTime(timezone=True)),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_changed_at", DateTime(timezone=True), nullable=False, index=True),
    Column("closed_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('active', 'closed')", name="ck_job_postings_status"),
    CheckConstraint("consecutive_complete_misses >= 0", name="ck_job_postings_misses"),
    UniqueConstraint(
        "provider", "career_source_id", "external_job_id", name="uq_job_posting_identity"
    ),
)

job_posting_versions_table = Table(
    "job_posting_versions",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("job_posting_id", BigInteger, ForeignKey("job_postings.id"), nullable=False, index=True),
    Column(
        "source_sync_run_id",
        BigInteger,
        ForeignKey("source_sync_runs.id"),
        nullable=False,
        index=True,
    ),
    Column("content_hash", String, nullable=False, index=True),
    Column("title", Text, nullable=False),
    Column("description_html", Text),
    Column("description_text", Text),
    Column("location", Text),
    Column("department", Text),
    Column("employment_type", Text),
    Column("posting_url", Text),
    Column("apply_url", Text),
    Column("source_published_at", DateTime(timezone=True)),
    Column("source_updated_at", DateTime(timezone=True)),
    Column("raw_payload", JSONB, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("job_posting_id", "source_sync_run_id", name="uq_job_posting_version_run"),
)

job_posting_observations_table = Table(
    "job_posting_observations",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("job_posting_id", BigInteger, ForeignKey("job_postings.id"), nullable=False, index=True),
    Column(
        "source_sync_run_id",
        BigInteger,
        ForeignKey("source_sync_runs.id"),
        nullable=False,
        index=True,
    ),
    Column("observation_kind", String, nullable=False),
    Column("status_before", String, nullable=False),
    Column("status_after", String, nullable=False),
    Column("content_hash", String),
    Column("job_posting_version_id", BigInteger, ForeignKey("job_posting_versions.id")),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("evidence", JSONB, nullable=False, default=dict),
    CheckConstraint("observation_kind IN ('seen', 'missed')", name="ck_job_observation_kind"),
    CheckConstraint(
        "status_before IN ('active', 'closed')", name="ck_job_observation_status_before"
    ),
    CheckConstraint("status_after IN ('active', 'closed')", name="ck_job_observation_status_after"),
    UniqueConstraint("source_sync_run_id", "job_posting_id", name="uq_job_observation_run_job"),
)

Index("ix_companies_slug", companies_table.c.slug, unique=True)
Index("ix_companies_normalized_name", companies_table.c.normalized_name)
Index("ix_companies_primary_domain", companies_table.c.primary_domain)
Index(
    "ix_yc_company_profiles_yc_company_id", yc_company_profiles_table.c.yc_company_id, unique=True
)
Index(
    "ix_career_page_discovery_statuses_company_slug",
    career_page_discovery_statuses_table.c.company_slug,
    unique=True,
)
Index(
    "ix_job_postings_company_active", job_postings_table.c.company_id, job_postings_table.c.status
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


@contextmanager
def url_inventory_writer_lock(engine: Engine) -> Iterator[None]:
    """Prevent a cleanup apply from racing a discovery/classification writer.

    Pipeline stages retain a shared session lock while they read or mutate URL
    inventory. Cleanup's exclusive session lock fails fast while either writer is
    running. Lightweight test doubles do not expose a SQLAlchemy Engine and are
    intentionally left lock-free.
    """
    if not isinstance(engine, Engine):
        yield
        return
    with engine.connect() as connection:
        locked = connection.scalar(
            text("SELECT pg_try_advisory_lock_shared(hashtext(:lock_name))"),
            {"lock_name": URL_INVENTORY_ADVISORY_LOCK},
        )
        if not locked:
            raise RuntimeError("URL cleanup apply is active; retry the pipeline stage later")
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock_shared(hashtext(:lock_name))"),
                {"lock_name": URL_INVENTORY_ADVISORY_LOCK},
            )


def create_schema(engine: Engine, *, checkfirst: bool = True) -> None:
    """Compatibility entry point backed by Alembic, the sole schema authority."""
    del checkfirst
    from yc_radar.services.migrations import upgrade_database

    upgrade_database(engine)


def rebuild_database(engine: Engine) -> None:
    """Destructively rebuild the schema through migration history."""
    from yc_radar.services.migrations import rebuild_database as rebuild_with_migrations

    rebuild_with_migrations(engine)


def has_companies(engine: Engine) -> bool:
    create_schema(engine)
    with engine.connect() as connection:
        return bool(connection.scalar(select(func.count()).select_from(companies_table)))


def upsert_yc_companies(engine: Engine, companies: list[dict[str, Any]]) -> None:
    """Upsert YC data without treating YC's external numeric ID as a local company ID.

    Existing YC provider identity determines the local company row. A new YC identity may reuse a
    source-neutral employer only when the verified primary domain and normalized name identify one
    non-YC company unambiguously; otherwise it receives a new local ID.
    """
    create_schema(engine)
    if not companies:
        return
    now = datetime.now(UTC)
    with engine.begin() as connection:
        incoming_yc_ids = {str(_yc_company_id(company)) for company in companies}
        persisted_company_rows = [
            dict(row)
            for row in connection.execute(
                select(
                    companies_table.c.id,
                    companies_table.c.name,
                    companies_table.c.website,
                    companies_table.c.primary_domain,
                )
            ).mappings()
        ]
        yc_source_ids_by_company: dict[int, set[str]] = {}
        yc_source_owner_by_id: dict[str, int] = {}
        for row in connection.execute(
            select(
                company_sources_table.c.company_id,
                company_sources_table.c.external_company_id,
            ).where(company_sources_table.c.provider == "yc")
        ):
            company_id = int(row.company_id)
            external_id = str(row.external_company_id)
            yc_source_ids_by_company.setdefault(company_id, set()).add(external_id)
            yc_source_owner_by_id[external_id] = company_id

        persisted_by_domain: dict[str, list[dict[str, Any]]] = {}
        for row in persisted_company_rows:
            domain = str(row.get("primary_domain") or "")
            if domain:
                persisted_by_domain.setdefault(domain, []).append(row)

        neutral_companies = sanitized_yc_company_payloads(companies)
        for company, neutral_company in zip(companies, neutral_companies, strict=True):
            domain = primary_domain_for_website(neutral_company.get("website"))
            if not domain:
                continue
            current_company_id = yc_source_owner_by_id.get(str(_yc_company_id(company)))
            standalone_owners = [
                row
                for row in persisted_by_domain.get(domain, [])
                if int(row["id"]) != current_company_id
                and not yc_source_ids_by_company.get(int(row["id"]))
            ]
            exact_standalone_match = len(standalone_owners) == 1 and normalize_company_name(
                str(standalone_owners[0]["name"])
            ) == normalize_company_name(str(neutral_company.get("name") or ""))
            if standalone_owners and not exact_standalone_match:
                neutral_company["website"] = None
                neutral_company["_website_domain_conflict"] = True

        persisted_claims = [
            {
                "id": f"persisted:{row['id']}",
                "name": row["name"],
                "website": row["website"],
                "_local_company_id": int(row["id"]),
            }
            for row in persisted_company_rows
            if (source_ids := yc_source_ids_by_company.get(int(row["id"])))
            and source_ids.isdisjoint(incoming_yc_ids)
        ]
        sanitized_claims = sanitized_yc_company_payloads(neutral_companies + persisted_claims)
        neutral_companies = sanitized_claims[: len(neutral_companies)]
        for persisted, sanitized in zip(
            persisted_claims,
            sanitized_claims[len(companies) :],
            strict=True,
        ):
            if sanitized.get("_website_domain_conflict"):
                connection.execute(
                    companies_table.update()
                    .where(companies_table.c.id == persisted["_local_company_id"])
                    .values(website=None, primary_domain=None, updated_at=now)
                )

        for company, neutral_company in zip(companies, neutral_companies, strict=True):
            yc_company_id = _yc_company_id(company)
            existing_source = (
                connection.execute(
                    select(company_sources_table).where(
                        company_sources_table.c.provider == "yc",
                        company_sources_table.c.external_company_id == str(yc_company_id),
                    )
                )
                .mappings()
                .first()
            )
            local_company_id = (
                int(existing_source["company_id"])
                if existing_source is not None
                else _matching_neutral_company_id(connection, neutral_company)
            )
            desired_slug = (
                str(
                    connection.scalar(
                        select(companies_table.c.slug).where(
                            companies_table.c.id == local_company_id
                        )
                    )
                )
                if local_company_id is not None
                else _available_company_slug(
                    connection,
                    requested_slug=str(company.get("slug") or ""),
                    yc_company_id=yc_company_id,
                )
            )
            if local_company_id is None:
                neutral_values = _company_row(neutral_company, slug=desired_slug, now=now)
                local_company_id = int(
                    connection.execute(
                        companies_table.insert()
                        .values(neutral_values)
                        .returning(companies_table.c.id)
                    ).scalar_one()
                )
            else:
                existing_company = (
                    connection.execute(
                        select(companies_table).where(companies_table.c.id == local_company_id)
                    )
                    .mappings()
                    .one()
                )
                neutral_values = _company_row(
                    neutral_company,
                    local_company_id=local_company_id,
                    slug=desired_slug,
                    now=now,
                )
                if (
                    neutral_values["website"] is None
                    and not neutral_company.get("_website_domain_conflict")
                    and sanitized_yc_company_website(existing_company) is not None
                ):
                    neutral_values["website"] = existing_company["website"]
                    neutral_values["primary_domain"] = existing_company["primary_domain"]
                statement = pg_insert(companies_table).values(neutral_values)
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["id"],
                        set_=_upsert_update_columns(statement, companies_table),
                    )
                )

            profile = _yc_company_profile_row(
                company,
                local_company_id=local_company_id,
                yc_company_id=yc_company_id,
                now=now,
            )
            profile_statement = pg_insert(yc_company_profiles_table).values(profile)
            connection.execute(
                profile_statement.on_conflict_do_update(
                    index_elements=["company_id"],
                    set_=_upsert_update_columns(profile_statement, yc_company_profiles_table),
                )
            )
            source_values = {
                "company_id": local_company_id,
                "provider": "yc",
                "external_company_id": str(yc_company_id),
                "source_url": profile["yc_url"],
                "raw_json": {"provider": "yc"},
                "first_seen_at": now,
                "last_seen_at": now,
                "created_at": now,
                "updated_at": now,
            }
            source_statement = pg_insert(company_sources_table).values(source_values)
            connection.execute(
                source_statement.on_conflict_do_update(
                    index_elements=["provider", "external_company_id"],
                    set_={
                        "source_url": source_statement.excluded.source_url,
                        "raw_json": source_statement.excluded.raw_json,
                        "last_seen_at": source_statement.excluded.last_seen_at,
                        "updated_at": source_statement.excluded.updated_at,
                    },
                )
            )
        _reset_companies_id_sequence(connection)


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
    with engine.begin() as connection:
        upsert_source_documents_connection(connection, documents)


def upsert_source_documents_connection(connection: Any, documents: list[dict[str, Any]]) -> None:
    """Upsert source documents into an existing transaction."""
    if not documents:
        return
    _upsert_rows_connection(
        connection,
        source_documents_table,
        [_source_document_row(document) for document in documents],
        index_elements=["source_type", "source_key"],
    )


def upsert_page_classifications(engine: Engine, classifications: list[dict[str, Any]]) -> None:
    create_schema(engine)
    if not classifications:
        return
    with engine.begin() as connection:
        upsert_page_classifications_connection(connection, classifications)


def upsert_page_classifications_connection(
    connection: Any, classifications: list[dict[str, Any]]
) -> None:
    """Upsert classifications into an existing transaction."""
    if not classifications:
        return
    _upsert_rows_connection(
        connection,
        page_classifications_table,
        [_page_classification_row(classification) for classification in classifications],
        index_elements=["source_document_id", "parser_name"],
    )


def upsert_external_job_postings(engine: Engine, jobs: list[dict[str, Any]]) -> None:
    create_schema(engine)
    if not jobs:
        return
    with engine.begin() as connection:
        upsert_external_job_postings_connection(connection, jobs)


def upsert_external_job_postings_connection(connection: Any, jobs: list[dict[str, Any]]) -> None:
    """Upsert derived URL jobs into an existing transaction."""
    if not jobs:
        return
    _upsert_rows_connection(
        connection,
        external_job_postings_table,
        [_external_job_row(job) for job in jobs],
        index_elements=["source", "normalized_url"],
    )


def upsert_career_page_discovery_statuses(
    engine: Engine,
    statuses: list[dict[str, Any]],
) -> None:
    create_schema(engine)
    if not statuses:
        return
    with engine.begin() as connection:
        upsert_career_page_discovery_statuses_connection(connection, statuses)


def upsert_career_page_discovery_statuses_connection(
    connection: Any,
    statuses: list[dict[str, Any]],
) -> None:
    if not statuses:
        return
    rows = [_career_page_discovery_status_row(status) for status in statuses]
    _upsert_rows_connection(
        connection,
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
    statuses: list[dict[str, Any]] | None = None,
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
        if statuses:
            upsert_career_page_discovery_statuses_connection(connection, statuses)


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


def _company_profile_statement(*, require_yc_profile: bool = False):
    profile_columns = (
        yc_company_profiles_table.c.yc_company_id,
        yc_company_profiles_table.c.yc_url,
        yc_company_profiles_table.c.one_liner,
        yc_company_profiles_table.c.batch,
        yc_company_profiles_table.c.status,
        yc_company_profiles_table.c.stage,
        yc_company_profiles_table.c.team_size,
        yc_company_profiles_table.c.is_hiring,
        yc_company_profiles_table.c.all_locations,
        yc_company_profiles_table.c.regions,
        yc_company_profiles_table.c.industry,
        yc_company_profiles_table.c.subindustry,
        yc_company_profiles_table.c.industries,
        yc_company_profiles_table.c.tags,
        yc_company_profiles_table.c.prototype_score,
        yc_company_profiles_table.c.prototype_angle,
        yc_company_profiles_table.c.raw_json,
    )
    statement = select(companies_table, *profile_columns)
    join = companies_table.c.id == yc_company_profiles_table.c.company_id
    return (
        statement.join(yc_company_profiles_table, join)
        if require_yc_profile
        else statement.outerjoin(yc_company_profiles_table, join)
    )


def fetch_company_rows(engine: Engine) -> list[dict[str, Any]]:
    create_schema(engine)
    with engine.connect() as connection:
        rows = connection.execute(_company_profile_statement()).mappings().all()
    return [dict(row) for row in rows]


def fetch_company_row(engine: Engine, slug: str) -> dict[str, Any] | None:
    create_schema(engine)
    with engine.connect() as connection:
        row = (
            connection.execute(
                _company_profile_statement().where(companies_table.c.slug == slug.lower())
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def fetch_companies_for_discovery(
    engine: Engine,
    *,
    limit: int | None = None,
    company_slugs: list[str] | None = None,
    only_pending: bool = False,
    hiring_only: bool = False,
    source_provider: str | None = None,
) -> list[dict[str, Any]]:
    """Select discovery candidates, applying completed-status exclusion before LIMIT."""
    create_schema(engine)
    statement = _company_profile_statement()
    if company_slugs:
        statement = statement.where(companies_table.c.slug.in_(company_slugs))
    if only_pending:
        completed = select(career_page_discovery_statuses_table.c.id).where(
            career_page_discovery_statuses_table.c.company_slug == companies_table.c.slug,
            career_page_discovery_statuses_table.c.status == "completed",
        )
        statement = statement.where(~completed.exists())
    if hiring_only:
        statement = statement.where(yc_company_profiles_table.c.is_hiring.is_(True))
    if source_provider:
        source_exists = select(company_sources_table.c.id).where(
            company_sources_table.c.company_id == companies_table.c.id,
            company_sources_table.c.provider == source_provider.lower(),
        )
        statement = statement.where(source_exists.exists())
    statement = statement.order_by(companies_table.c.slug)
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
    retry_fetch_errors: bool = False,
    max_fetch_attempts: int = 3,
) -> list[dict[str, Any]]:
    """Return active URL work after its mode-specific eligibility predicate.

    Retry eligibility is intentionally explicit: only classifications that recorded a
    retryable fetch policy are considered, and the attempt budget is enforced in SQL.
    """
    create_schema(engine)
    statement = select(discovered_urls_table).where(discovered_urls_table.c.is_active.is_(True))
    if company_slugs:
        statement = statement.where(discovered_urls_table.c.company_slug.in_(company_slugs))
    if retry_fetch_errors:
        fetch_data = page_classifications_table.c.evidence["fetch"]
        retryable = fetch_data["retryable"].astext == "true"
        attempts = cast(fetch_data["attempt_count"].astext, Integer)
        eligible = select(page_classifications_table.c.id).where(
            page_classifications_table.c.discovered_url_id == discovered_urls_table.c.id,
            page_classifications_table.c.page_kind == "fetch_error",
            retryable,
            attempts < max(1, max_fetch_attempts),
        )
        attempt_count = (
            select(attempts)
            .where(page_classifications_table.c.discovered_url_id == discovered_urls_table.c.id)
            .order_by(page_classifications_table.c.classified_at.desc())
            .limit(1)
            .scalar_subquery()
            .label("fetch_attempt_count")
        )
        statement = statement.add_columns(attempt_count).where(eligible.exists())
    elif only_unclassified:
        classified = select(page_classifications_table.c.id).where(
            page_classifications_table.c.discovered_url_id == discovered_urls_table.c.id
        )
        statement = statement.where(~classified.exists())
    else:
        statement = statement.add_columns(literal(0).label("fetch_attempt_count"))
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
    with engine.connect() as connection:
        return fetch_source_document_rows_connection(
            connection,
            source_type=source_type,
            source_keys=source_keys,
        )


def fetch_source_document_rows_connection(
    connection: Any,
    *,
    source_type: str | None = None,
    source_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Read source documents through an existing transaction."""
    statement = select(source_documents_table)
    if source_type:
        statement = statement.where(source_documents_table.c.source_type == source_type)
    if source_keys:
        statement = statement.where(source_documents_table.c.source_key.in_(source_keys))
    statement = statement.order_by(
        source_documents_table.c.company_slug, source_documents_table.c.id
    )
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
        _upsert_rows_connection(connection, table, rows, index_elements=index_elements)


def _upsert_rows_connection(
    connection: Any,
    table: Table,
    rows: list[dict[str, Any]],
    *,
    index_elements: list[str],
) -> None:
    for chunk in _chunks(rows, BATCH_SIZE):
        statement = pg_insert(table).values(chunk)
        update_columns = _upsert_update_columns(statement, table)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=index_elements,
                set_=update_columns,
            )
        )


def _reset_companies_id_sequence(connection: Any) -> None:
    connection.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('companies', 'id'), "
            "COALESCE((SELECT MAX(id) FROM companies), 1), "
            "(SELECT MAX(id) IS NOT NULL FROM companies))"
        )
    )


def _upsert_update_columns(statement: Any, table: Table) -> dict[str, Any]:
    return {
        column.name: getattr(statement.excluded, column.name)
        for column in table.columns
        if column.name not in {"id", "created_at"} and column.computed is None
    }


def normalize_company_name(value: str) -> str:
    return " ".join(value.lower().split())


def primary_domain_for_website(website: str | None) -> str | None:
    if not website:
        return None
    try:
        host = (urlparse(website).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    domain = host.removeprefix("www.")
    return domain if _DOMAIN_PATTERN.fullmatch(domain) else None


def sanitized_yc_company_website(company: dict[str, Any]) -> str | None:
    """Return a website suitable for the source-neutral company row.

    YC's raw profile payload sometimes puts a directory listing, social profile, app-store page,
    or even multiple URLs in ``website``. Keep that evidence in the YC profile JSON, but do not
    promote it to the neutral employer identity.
    """
    raw_website = company.get("website")
    if not isinstance(raw_website, str):
        return None
    website = raw_website.strip()
    if not website or any(character.isspace() for character in website):
        return None
    if _DOMAIN_PATTERN.fullmatch(website):
        website = f"https://{website}"
    elif len(_ABSOLUTE_HTTP_URL_PATTERN.findall(website)) != 1:
        return None

    try:
        parsed = urlparse(website)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or host.endswith(".")
        or parsed.netloc.endswith(":")
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
        or not _DOMAIN_PATTERN.fullmatch(host.removeprefix("www."))
    ):
        return None

    shared_host = next(
        (
            candidate
            for candidate in _SHARED_WEBSITE_HOSTS
            if host.removeprefix("www.") == candidate
            or host.removeprefix("www.").endswith(f".{candidate}")
        ),
        None,
    )
    if shared_host is None:
        return website

    normalized_name = normalize_company_name(str(company.get("name") or "").strip())
    is_allowed_brand_root = (
        host.removeprefix("www.") == shared_host
        and _SHARED_ROOT_WEBSITE_BRANDS.get(shared_host) == normalized_name
        and parsed.path in {"", "/"}
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )
    return website if is_allowed_brand_root else None


def sanitized_yc_company_payloads(
    companies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Copy YC payloads with neutral websites deconflicted across the batch.

    A domain claimed by multiple YC identities is not safe identity evidence. If
    exactly one claimant has a deterministic brand-shaped domain, retain that
    claimant and clear the others; otherwise clear every competing claim. Raw YC
    profile payloads are written from the original list and remain unchanged.
    """

    sanitized = [
        {**company, "website": sanitized_yc_company_website(company)} for company in companies
    ]
    claims: dict[str, list[int]] = {}
    for index, company in enumerate(sanitized):
        domain = primary_domain_for_website(company.get("website"))
        if domain:
            claims.setdefault(domain, []).append(index)

    for domain, indexes in claims.items():
        if len(indexes) < 2:
            continue
        compatible = [
            index
            for index in indexes
            if _company_name_matches_domain(str(sanitized[index].get("name") or ""), domain)
        ]
        survivor = compatible[0] if len(compatible) == 1 else None
        for index in indexes:
            if index != survivor:
                sanitized[index]["website"] = None
                sanitized[index]["_website_domain_conflict"] = True
    return sanitized


def _company_name_matches_domain(name: str, domain: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", normalize_company_name(name))
    legal_suffixes = {
        "incorporated",
        "corporation",
        "limited",
        "corp",
        "llc",
        "ltd",
        "inc",
    }
    if len(tokens) > 1 and tokens[-1] in legal_suffixes:
        tokens.pop()
    normalized = "".join(tokens)
    label = domain.split(".", 1)[0].lower()
    if not normalized or not label:
        return False
    if label == normalized:
        return True
    return any(
        label == f"{prefix}{normalized}"
        for prefix in ("get", "go", "hey", "join", "my", "ridewith", "team", "try", "use")
    )


def _yc_company_id(company: dict[str, Any]) -> int:
    yc_company_id = _to_int(company.get("id")) or _to_int(company.get("objectID"))
    if yc_company_id is None:
        raise ValueError("YC company requires id or objectID")
    return yc_company_id


def _matching_neutral_company_id(connection: Any, company: dict[str, Any]) -> int | None:
    """Return one safe non-YC identity match, never a best-effort merge."""
    domain = primary_domain_for_website(sanitized_yc_company_website(company))
    normalized_name = normalize_company_name(str(company.get("name") or "").strip())
    if not domain or not normalized_name:
        return None
    domain_matches = list(
        connection.execute(
            select(companies_table).where(companies_table.c.primary_domain == domain)
        ).mappings()
    )
    if len(domain_matches) != 1:
        return None
    candidate = domain_matches[0]
    if candidate["normalized_name"] != normalized_name:
        return None
    existing_profile = connection.scalar(
        select(yc_company_profiles_table.c.company_id).where(
            yc_company_profiles_table.c.company_id == candidate["id"]
        )
    )
    existing_yc_source = connection.scalar(
        select(company_sources_table.c.id).where(
            company_sources_table.c.company_id == candidate["id"],
            company_sources_table.c.provider == "yc",
        )
    )
    if existing_profile is not None or existing_yc_source is not None:
        return None
    return int(candidate["id"])


def _available_company_slug(
    connection: Any,
    *,
    requested_slug: str,
    yc_company_id: int,
) -> str:
    base = requested_slug.strip().lower() or f"yc-{yc_company_id}"
    for candidate in (base, f"{base}-yc-{yc_company_id}"):
        owner = connection.scalar(
            select(companies_table.c.id).where(companies_table.c.slug == candidate)
        )
        if owner is None:
            return candidate
    suffix = 2
    while True:
        candidate = f"{base}-yc-{yc_company_id}-{suffix}"
        owner = connection.scalar(
            select(companies_table.c.id).where(companies_table.c.slug == candidate)
        )
        if owner is None:
            return candidate
        suffix += 1


def _company_row(
    company: dict[str, Any],
    *,
    slug: str,
    now: datetime,
    local_company_id: int | None = None,
) -> dict[str, Any]:
    name = str(company.get("name") or "").strip()
    website = sanitized_yc_company_website(company)
    row: dict[str, Any] = {
        "name": name,
        "normalized_name": normalize_company_name(name),
        "slug": slug,
        "website": website,
        "primary_domain": primary_domain_for_website(website),
        "created_at": now,
        "updated_at": now,
    }
    if local_company_id is not None:
        row["id"] = local_company_id
    return row


def _yc_company_profile_row(
    company: dict[str, Any],
    *,
    local_company_id: int,
    yc_company_id: int,
    now: datetime,
) -> dict[str, Any]:
    slug = str(company.get("slug") or "").lower()
    return {
        "company_id": local_company_id,
        "yc_company_id": yc_company_id,
        "yc_url": f"https://www.ycombinator.com/companies/{slug}",
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
    return canonical_url_key(url) or url


def _chunks(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]
