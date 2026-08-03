from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.engine import Engine, make_url

from yc_radar.core.config import get_settings


metadata = MetaData()
INGEST_SCHEMA = "ingest"

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
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False),
    Column("normalized_name", Text, nullable=False),
    Column("slug", Text, nullable=False),
    Column("website", Text),
    Column("primary_domain", Text),
    Column("identity_state", Text, nullable=False, default="verified", server_default="verified"),
    Column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "identity_state IN ('verified', 'provisional')",
        name="ck_companies_identity_state",
    ),
    UniqueConstraint("slug", name="uq_companies_slug"),
)

company_sources_table = Table(
    "company_sources",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "company_id",
        BigInteger,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("provider", Text, nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("external_id", Text, nullable=False),
    Column("source_url", Text),
    Column("sync_mode", Text, nullable=False),
    Column("status", Text, nullable=False, default="active"),
    Column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "sync_mode IN ('none', 'complete_snapshot', 'observation')",
        name="ck_company_sources_sync_mode",
    ),
    CheckConstraint(
        "status IN ('active', 'disabled')",
        name="ck_company_sources_status",
    ),
    UniqueConstraint(
        "provider",
        "external_id",
        name="uq_company_sources_provider_external_id",
    ),
)

sync_runs_table = Table(
    "sync_runs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "company_source_id",
        BigInteger,
        ForeignKey("company_sources.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("run_key", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("is_complete", Boolean, nullable=False),
    Column("stats", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column("details", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('running', 'completed', 'partial', 'failed')",
        name="ck_sync_runs_status",
    ),
    UniqueConstraint(
        "company_source_id",
        "run_key",
        name="uq_sync_runs_source_run_key",
    ),
)

jobs_table = Table(
    "jobs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "company_source_id",
        BigInteger,
        ForeignKey("company_sources.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("external_job_id", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("posting_url", Text),
    Column("apply_url", Text),
    Column("description_text", Text),
    Column("location", Text),
    Column("department", Text),
    Column("employment_type", Text),
    Column(
        "structured_evidence",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    ),
    Column(
        "raw_payload",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    ),
    Column("status", Text, nullable=False, default="active"),
    Column("consecutive_complete_misses", Integer, nullable=False, default=0, server_default="0"),
    Column("content_hash", Text, nullable=False),
    Column("source_published_at", DateTime(timezone=True)),
    Column("source_updated_at", DateTime(timezone=True)),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_changed_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True)),
    Column("last_seen_run_id", BigInteger, ForeignKey("sync_runs.id", ondelete="SET NULL")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('active', 'closed')", name="ck_jobs_status"),
    CheckConstraint(
        "consecutive_complete_misses >= 0",
        name="ck_jobs_nonnegative_misses",
    ),
    UniqueConstraint(
        "company_source_id",
        "external_job_id",
        name="uq_jobs_source_external_job_id",
    ),
)

ingest_runs_table = Table(
    "runs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_key", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("status", Text, nullable=False, default="running", server_default=text("'running'")),
    Column("parser_version", Text, nullable=False),
    Column("normalizer_version", Text, nullable=False),
    Column("input_uri", Text),
    Column("input_sha256", Text),
    Column("cursor", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column("stats", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('running', 'completed', 'partial', 'failed')",
        name="ck_ingest_runs_status",
    ),
    CheckConstraint(
        "char_length(run_key) BETWEEN 1 AND 512",
        name="ck_ingest_runs_run_key_length",
    ),
    CheckConstraint(
        "char_length(source) BETWEEN 1 AND 128",
        name="ck_ingest_runs_source_length",
    ),
    CheckConstraint(
        "char_length(parser_version) BETWEEN 1 AND 128",
        name="ck_ingest_runs_parser_version_length",
    ),
    CheckConstraint(
        "char_length(normalizer_version) BETWEEN 1 AND 128",
        name="ck_ingest_runs_normalizer_version_length",
    ),
    CheckConstraint(
        "input_uri IS NULL OR char_length(input_uri) BETWEEN 1 AND 8192",
        name="ck_ingest_runs_input_uri_length",
    ),
    CheckConstraint(
        "input_sha256 IS NULL OR input_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_ingest_runs_input_sha256",
    ),
    CheckConstraint(
        "jsonb_typeof(cursor) = 'object' AND pg_column_size(cursor) <= 262144",
        name="ck_ingest_runs_cursor",
    ),
    CheckConstraint(
        "jsonb_typeof(stats) = 'object' AND pg_column_size(stats) <= 262144",
        name="ck_ingest_runs_stats",
    ),
    CheckConstraint(
        "(status = 'running' AND completed_at IS NULL) OR "
        "(status <> 'running' AND completed_at IS NOT NULL)",
        name="ck_ingest_runs_completion",
    ),
    UniqueConstraint("source", "run_key", name="uq_ingest_runs_source_run_key"),
    schema=INGEST_SCHEMA,
)

ingest_raw_observations_table = Table(
    "raw_observations",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "run_id",
        BigInteger,
        ForeignKey(
            "ingest.runs.id",
            name="fk_ingest_raw_observations_run_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column(
        "url_work_item_id",
        BigInteger,
        ForeignKey(
            "ingest.url_work_items.id",
            name="fk_ingest_raw_observations_url_work_item_id",
            ondelete="SET NULL",
        ),
    ),
    Column("observation_key", Text, nullable=False),
    Column("observed_url", Text),
    Column("payload", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "char_length(observation_key) BETWEEN 1 AND 512",
        name="ck_ingest_raw_observations_key_length",
    ),
    CheckConstraint(
        "observed_url IS NULL OR char_length(observed_url) BETWEEN 1 AND 8192",
        name="ck_ingest_raw_observations_url_length",
    ),
    CheckConstraint(
        "jsonb_typeof(payload) = 'object' AND pg_column_size(payload) <= 1048576",
        name="ck_ingest_raw_observations_payload",
    ),
    UniqueConstraint(
        "run_id",
        "observation_key",
        name="uq_ingest_raw_observations_run_key",
    ),
    schema=INGEST_SCHEMA,
)

ingest_url_work_items_table = Table(
    "url_work_items",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "run_id",
        BigInteger,
        ForeignKey(
            "ingest.runs.id",
            name="fk_ingest_url_work_items_run_id",
            ondelete="SET NULL",
        ),
    ),
    Column("normalized_url", Text, nullable=False),
    Column("host", Text, nullable=False),
    Column("stage", Text, nullable=False, default="fetch", server_default=text("'fetch'")),
    Column("state", Text, nullable=False, default="ready", server_default=text("'ready'")),
    Column("priority", Integer, nullable=False, default=0, server_default="0"),
    Column("attempt_count", Integer, nullable=False, default=0, server_default="0"),
    Column("max_attempts", Integer, nullable=False, default=5, server_default="5"),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", Text),
    Column("lease_token", Text),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("artifact_uri", Text),
    Column("http_status", Integer),
    Column("content_type", Text),
    Column("content_hash", Text),
    Column("result", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column(
        "last_error",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    ),
    Column("parser_version", Text, nullable=False),
    Column("normalizer_version", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "stage IN ('fetch', 'parse', 'enrich', 'promote', 'done')",
        name="ck_ingest_url_work_items_stage",
    ),
    CheckConstraint(
        "state IN "
        "('ready', 'leased', 'retry', 'verified', 'promoted', 'quarantined', 'dead')",
        name="ck_ingest_url_work_items_state",
    ),
    CheckConstraint(
        "priority BETWEEN -1000000 AND 1000000",
        name="ck_ingest_url_work_items_priority",
    ),
    CheckConstraint(
        "attempt_count >= 0 AND max_attempts BETWEEN 1 AND 100 "
        "AND attempt_count <= max_attempts",
        name="ck_ingest_url_work_items_attempts",
    ),
    CheckConstraint(
        "(state = 'leased' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) OR "
        "(state <> 'leased' AND lease_owner IS NULL AND lease_token IS NULL "
        "AND lease_expires_at IS NULL)",
        name="ck_ingest_url_work_items_lease",
    ),
    CheckConstraint(
        "char_length(normalized_url) BETWEEN 1 AND 2048",
        name="ck_ingest_url_work_items_url_length",
    ),
    CheckConstraint(
        "char_length(host) BETWEEN 1 AND 253",
        name="ck_ingest_url_work_items_host_length",
    ),
    CheckConstraint(
        "lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 256",
        name="ck_ingest_url_work_items_lease_owner_length",
    ),
    CheckConstraint(
        "lease_token IS NULL OR char_length(lease_token) BETWEEN 1 AND 512",
        name="ck_ingest_url_work_items_lease_token_length",
    ),
    CheckConstraint(
        "artifact_uri IS NULL OR char_length(artifact_uri) BETWEEN 1 AND 8192",
        name="ck_ingest_url_work_items_artifact_uri_length",
    ),
    CheckConstraint(
        "http_status IS NULL OR http_status BETWEEN 100 AND 599",
        name="ck_ingest_url_work_items_http_status",
    ),
    CheckConstraint(
        "content_type IS NULL OR char_length(content_type) BETWEEN 1 AND 255",
        name="ck_ingest_url_work_items_content_type_length",
    ),
    CheckConstraint(
        "content_hash IS NULL OR char_length(content_hash) BETWEEN 1 AND 256",
        name="ck_ingest_url_work_items_content_hash_length",
    ),
    CheckConstraint(
        "char_length(parser_version) BETWEEN 1 AND 128",
        name="ck_ingest_url_work_items_parser_version_length",
    ),
    CheckConstraint(
        "char_length(normalizer_version) BETWEEN 1 AND 128",
        name="ck_ingest_url_work_items_normalizer_version_length",
    ),
    CheckConstraint(
        "jsonb_typeof(result) = 'object' AND pg_column_size(result) <= 262144",
        name="ck_ingest_url_work_items_result",
    ),
    CheckConstraint(
        "jsonb_typeof(last_error) = 'object' AND pg_column_size(last_error) <= 262144",
        name="ck_ingest_url_work_items_last_error",
    ),
    UniqueConstraint(
        "normalized_url",
        "parser_version",
        "normalizer_version",
        name="uq_ingest_url_work_items_url_versions",
    ),
    schema=INGEST_SCHEMA,
)

ingest_job_candidates_table = Table(
    "job_candidates",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "run_id",
        BigInteger,
        ForeignKey(
            "ingest.runs.id",
            name="fk_ingest_job_candidates_run_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column(
        "raw_observation_id",
        BigInteger,
        ForeignKey(
            "ingest.raw_observations.id",
            name="fk_ingest_job_candidates_raw_observation_id",
        ),
        nullable=False,
    ),
    Column(
        "work_item_id",
        BigInteger,
        ForeignKey(
            "ingest.url_work_items.id",
            name="fk_ingest_job_candidates_work_item_id",
            ondelete="SET NULL",
        ),
    ),
    Column("candidate_key", Text, nullable=False),
    Column(
        "company_source_id",
        BigInteger,
        ForeignKey(
            "company_sources.id",
            name="fk_ingest_job_candidates_company_source_id",
            ondelete="RESTRICT",
        ),
    ),
    Column("provider", Text),
    Column("external_source_id", Text),
    Column("external_job_id", Text),
    Column("snapshot_complete", Boolean, nullable=False, default=False, server_default="false"),
    Column("title", Text),
    Column("posting_url", Text),
    Column("apply_url", Text),
    Column("description_text", Text),
    Column("location", Text),
    Column("department", Text),
    Column("employment_type", Text),
    Column("content_hash", Text),
    Column("source_published_at", DateTime(timezone=True)),
    Column("source_updated_at", DateTime(timezone=True)),
    Column(
        "field_provenance",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    ),
    Column(
        "quality_flags",
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    ),
    Column("payload", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column(
        "status",
        Text,
        nullable=False,
        default="normalized",
        server_default=text("'normalized'"),
    ),
    Column("parser_version", Text, nullable=False),
    Column("normalizer_version", Text, nullable=False),
    Column("error", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column(
        "promoted_job_id",
        BigInteger,
        ForeignKey(
            "jobs.id",
            name="fk_ingest_job_candidates_promoted_job_id",
            ondelete="SET NULL",
        ),
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "status IN ('normalized', 'ready', 'quarantined', 'promoted', 'rejected')",
        name="ck_ingest_job_candidates_status",
    ),
    CheckConstraint(
        "raw_observation_id IS NOT NULL",
        name="ck_ingest_job_candidates_lineage",
    ),
    CheckConstraint(
        "char_length(candidate_key) BETWEEN 1 AND 512",
        name="ck_ingest_job_candidates_key_length",
    ),
    CheckConstraint(
        "provider IS NULL OR char_length(provider) BETWEEN 1 AND 128",
        name="ck_ingest_job_candidates_provider_length",
    ),
    CheckConstraint(
        "external_source_id IS NULL OR char_length(external_source_id) BETWEEN 1 AND 512",
        name="ck_ingest_job_candidates_external_source_id_length",
    ),
    CheckConstraint(
        "external_job_id IS NULL OR char_length(external_job_id) BETWEEN 1 AND 512",
        name="ck_ingest_job_candidates_external_job_id_length",
    ),
    CheckConstraint(
        "title IS NULL OR char_length(title) BETWEEN 1 AND 1000",
        name="ck_ingest_job_candidates_title_length",
    ),
    CheckConstraint(
        "posting_url IS NULL OR char_length(posting_url) BETWEEN 1 AND 8192",
        name="ck_ingest_job_candidates_posting_url_length",
    ),
    CheckConstraint(
        "apply_url IS NULL OR char_length(apply_url) BETWEEN 1 AND 8192",
        name="ck_ingest_job_candidates_apply_url_length",
    ),
    CheckConstraint(
        "description_text IS NULL OR octet_length(description_text) <= 1048576",
        name="ck_ingest_job_candidates_description_size",
    ),
    CheckConstraint(
        "location IS NULL OR char_length(location) BETWEEN 1 AND 2000",
        name="ck_ingest_job_candidates_location_length",
    ),
    CheckConstraint(
        "department IS NULL OR char_length(department) BETWEEN 1 AND 1000",
        name="ck_ingest_job_candidates_department_length",
    ),
    CheckConstraint(
        "employment_type IS NULL OR char_length(employment_type) BETWEEN 1 AND 512",
        name="ck_ingest_job_candidates_employment_type_length",
    ),
    CheckConstraint(
        "content_hash IS NULL OR char_length(content_hash) BETWEEN 1 AND 256",
        name="ck_ingest_job_candidates_content_hash_length",
    ),
    CheckConstraint(
        "char_length(parser_version) BETWEEN 1 AND 128",
        name="ck_ingest_job_candidates_parser_version_length",
    ),
    CheckConstraint(
        "char_length(normalizer_version) BETWEEN 1 AND 128",
        name="ck_ingest_job_candidates_normalizer_version_length",
    ),
    CheckConstraint(
        "jsonb_typeof(field_provenance) = 'object' "
        "AND pg_column_size(field_provenance) <= 262144",
        name="ck_ingest_job_candidates_field_provenance",
    ),
    CheckConstraint(
        "jsonb_typeof(quality_flags) = 'array' "
        "AND pg_column_size(quality_flags) <= 262144",
        name="ck_ingest_job_candidates_quality_flags",
    ),
    CheckConstraint(
        "jsonb_typeof(payload) = 'object' AND pg_column_size(payload) <= 1048576",
        name="ck_ingest_job_candidates_payload",
    ),
    CheckConstraint(
        "jsonb_typeof(error) = 'object' AND pg_column_size(error) <= 262144",
        name="ck_ingest_job_candidates_error",
    ),
    CheckConstraint(
        "status NOT IN ('ready', 'promoted') OR "
        "(provider IS NOT NULL AND btrim(provider) <> '' "
        "AND external_source_id IS NOT NULL AND btrim(external_source_id) <> '' "
        "AND external_job_id IS NOT NULL AND btrim(external_job_id) <> '' "
        "AND title IS NOT NULL AND btrim(title) <> '')",
        name="ck_ingest_job_candidates_ready_fields",
    ),
    CheckConstraint(
        "status <> 'promoted' OR company_source_id IS NOT NULL",
        name="ck_ingest_job_candidates_promoted_source",
    ),
    CheckConstraint(
        "promoted_job_id IS NULL OR status = 'promoted'",
        name="ck_ingest_job_candidates_promoted_job",
    ),
    UniqueConstraint(
        "run_id",
        "candidate_key",
        name="uq_ingest_job_candidates_run_key",
    ),
    schema=INGEST_SCHEMA,
)

Index("ix_companies_normalized_name", companies_table.c.normalized_name)
Index("ix_companies_primary_domain", companies_table.c.primary_domain)
Index("ix_companies_identity_state", companies_table.c.identity_state)
Index("ix_company_sources_company_id", company_sources_table.c.company_id)
Index("ix_company_sources_provider", company_sources_table.c.provider)
Index("ix_company_sources_status", company_sources_table.c.status)
Index("ix_sync_runs_company_source_id", sync_runs_table.c.company_source_id)
Index("ix_sync_runs_started_at", sync_runs_table.c.started_at)
Index("ix_jobs_company_source_id", jobs_table.c.company_source_id)
Index("ix_jobs_status", jobs_table.c.status)
Index("ix_jobs_last_changed_at", jobs_table.c.last_changed_at)
Index("ix_jobs_last_seen_run_id", jobs_table.c.last_seen_run_id)
Index("ix_ingest_runs_started_at", ingest_runs_table.c.started_at)
Index(
    "ix_ingest_runs_running",
    ingest_runs_table.c.started_at,
    ingest_runs_table.c.id,
    postgresql_where=text("status = 'running'"),
)
Index(
    "ix_ingest_raw_observations_url_work_item_id",
    ingest_raw_observations_table.c.url_work_item_id,
    postgresql_where=text("url_work_item_id IS NOT NULL"),
)
Index(
    "ix_ingest_raw_observations_observed_at",
    ingest_raw_observations_table.c.observed_at,
)
Index(
    "ix_ingest_url_work_items_host",
    ingest_url_work_items_table.c.host,
)
Index(
    "ix_ingest_url_work_items_queue",
    ingest_url_work_items_table.c.priority.desc(),
    ingest_url_work_items_table.c.available_at,
    ingest_url_work_items_table.c.id,
    postgresql_where=text("state IN ('ready', 'retry') AND stage <> 'done'"),
)
Index(
    "ix_ingest_url_work_items_lease",
    ingest_url_work_items_table.c.lease_expires_at,
    ingest_url_work_items_table.c.id,
    postgresql_where=text("state = 'leased'"),
)
Index(
    "ix_ingest_job_candidates_raw_observation_id",
    ingest_job_candidates_table.c.raw_observation_id,
    postgresql_where=text("raw_observation_id IS NOT NULL"),
)
Index(
    "ix_ingest_job_candidates_work_item_id",
    ingest_job_candidates_table.c.work_item_id,
    postgresql_where=text("work_item_id IS NOT NULL"),
)
Index(
    "ix_ingest_job_candidates_ready",
    ingest_job_candidates_table.c.company_source_id,
    ingest_job_candidates_table.c.id,
    postgresql_where=text("status = 'ready'"),
)
Index(
    "ix_ingest_job_candidates_promoted_job_id",
    ingest_job_candidates_table.c.promoted_job_id,
    postgresql_where=text("promoted_job_id IS NOT NULL"),
)


def engine_from_url(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("YC Radar is Postgres-only. Set DATABASE_URL to a postgresql+psycopg URL.")
    return create_engine(parsed, future=True, pool_pre_ping=True)


def create_schema(engine: Engine, *, checkfirst: bool = True) -> None:
    """Upgrade a rebuildable local database to the single Alembic baseline."""
    del checkfirst
    from yc_radar.services.migrations import upgrade_database

    upgrade_database(engine)


def rebuild_database(engine: Engine) -> None:
    """Destructively recreate the core and ingest schemas through Alembic."""
    from yc_radar.services.migrations import rebuild_database as rebuild_with_migrations

    rebuild_with_migrations(engine)


def truncate_database(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE "
                "ingest.job_candidates, ingest.url_work_items, ingest.raw_observations, "
                "ingest.runs, jobs, sync_runs, company_sources, companies "
                "RESTART IDENTITY CASCADE"
            )
        )


def upsert_yc_companies(engine: Engine, companies: list[dict[str, Any]]) -> None:
    """Attach YC identities to canonical companies without unsafe name-only merging."""
    create_schema(engine)
    if not companies:
        return

    now = datetime.now(UTC)
    sanitized = sanitized_yc_company_payloads(companies)
    with engine.begin() as connection:
        for raw_company, neutral_company in zip(companies, sanitized, strict=True):
            yc_company_id = _yc_company_id(raw_company)
            external_id = str(yc_company_id)
            source = (
                connection.execute(
                    select(company_sources_table).where(
                        company_sources_table.c.provider == "yc",
                        company_sources_table.c.external_id == external_id,
                    )
                )
                .mappings()
                .first()
            )

            local_company_id = int(source["company_id"]) if source else None
            if local_company_id is None:
                local_company_id = _matching_neutral_company_id(connection, neutral_company)
            _isolate_conflicting_neutral_website(
                connection,
                neutral_company,
                exclude_company_id=local_company_id,
            )

            if local_company_id is None:
                slug = _available_company_slug(
                    connection,
                    requested_slug=str(raw_company.get("slug") or ""),
                    source_suffix=f"yc-{yc_company_id}",
                )
                local_company_id = int(
                    connection.execute(
                        companies_table.insert()
                        .values(_company_row(neutral_company, slug=slug, now=now))
                        .returning(companies_table.c.id)
                    ).scalar_one()
                )
            else:
                existing = (
                    connection.execute(
                        select(companies_table).where(companies_table.c.id == local_company_id)
                    )
                    .mappings()
                    .one()
                )
                incoming_website = sanitized_yc_company_website(neutral_company)
                if incoming_website is None:
                    incoming_website = existing["website"]
                values = _company_row(
                    {**neutral_company, "website": incoming_website},
                    slug=str(existing["slug"]),
                    now=now,
                    local_company_id=local_company_id,
                    company_metadata=dict(existing["metadata"] or {}),
                    created_at=existing["created_at"],
                )
                statement = pg_insert(companies_table).values(values)
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[companies_table.c.id],
                        set_={
                            "name": statement.excluded.name,
                            "normalized_name": statement.excluded.normalized_name,
                            "website": statement.excluded.website,
                            "primary_domain": statement.excluded.primary_domain,
                            "identity_state": "verified",
                            "updated_at": now,
                        },
                    )
                )

            slug = str(raw_company.get("slug") or "").strip().lower()
            source_values = {
                "company_id": local_company_id,
                "provider": "yc",
                "source_kind": "directory",
                "external_id": external_id,
                "source_url": f"https://www.ycombinator.com/companies/{slug}",
                "sync_mode": "complete_snapshot",
                "status": "active",
                "metadata": _yc_source_metadata(
                    raw_company,
                    identity_conflict_evidence=neutral_company.get(
                        "_identity_conflict_evidence"
                    ),
                ),
                "created_at": now,
                "updated_at": now,
            }
            source_statement = pg_insert(company_sources_table).values(source_values)
            connection.execute(
                source_statement.on_conflict_do_update(
                    index_elements=[
                        company_sources_table.c.provider,
                        company_sources_table.c.external_id,
                    ],
                    set_={
                        "source_url": source_statement.excluded.source_url,
                        "source_kind": source_statement.excluded.source_kind,
                        "sync_mode": source_statement.excluded.sync_mode,
                        "status": source_statement.excluded.status,
                        "metadata": source_statement.excluded.metadata,
                        "updated_at": now,
                    },
                )
            )


def fetch_company_rows(engine: Engine) -> list[dict[str, Any]]:
    create_schema(engine)
    with engine.connect() as connection:
        rows = connection.execute(_company_statement().order_by(companies_table.c.slug)).mappings()
        return [_project_company_row(dict(row)) for row in rows]


def fetch_company_row(engine: Engine, slug: str) -> dict[str, Any] | None:
    create_schema(engine)
    with engine.connect() as connection:
        row = (
            connection.execute(
                _company_statement().where(companies_table.c.slug == slug.strip().lower())
            )
            .mappings()
            .first()
        )
    return _project_company_row(dict(row)) if row else None


def normalize_company_name(value: str) -> str:
    return " ".join(value.casefold().split())


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
    """Return a safe canonical company website while preserving raw evidence in source metadata."""
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


def sanitized_yc_company_payloads(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize websites and remove ambiguous duplicate domain claims within a batch."""
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
                claimed_website = sanitized[index]["website"]
                conflicting_claimants = [
                    {
                        "external_id": str(
                            sanitized[other].get("id")
                            or sanitized[other].get("objectID")
                            or ""
                        ),
                        "name": str(sanitized[other].get("name") or ""),
                    }
                    for other in indexes
                    if other != index
                ]
                sanitized[index]["website"] = None
                sanitized[index]["_website_domain_conflict"] = True
                sanitized[index]["_identity_conflict_evidence"] = {
                    "kind": "website_domain_conflict",
                    "claimed_website": claimed_website,
                    "claimed_domain": domain,
                    "conflicting_incoming_companies": conflicting_claimants,
                }
    return sanitized


def _company_statement():
    yc_source = company_sources_table.alias("yc_source")
    return (
        select(
            companies_table,
            yc_source.c.external_id.label("yc_external_id"),
            yc_source.c.source_url.label("yc_source_url"),
            yc_source.c.metadata.label("yc_metadata"),
        )
        .select_from(companies_table)
        .outerjoin(
            yc_source,
            (yc_source.c.company_id == companies_table.c.id) & (yc_source.c.provider == "yc"),
        )
    )


def _project_company_row(row: dict[str, Any]) -> dict[str, Any]:
    source_metadata = dict(row.pop("yc_metadata", None) or {})
    raw_payload = source_metadata.pop("raw_payload", None)
    external_id = row.pop("yc_external_id", None)
    source_url = row.pop("yc_source_url", None)
    if external_id is not None:
        row.update(source_metadata)
        row["yc_company_id"] = _to_int(external_id)
        row["yc_url"] = source_url
        row["raw_json"] = raw_payload or {}
    return row


def _yc_source_metadata(
    company: dict[str, Any],
    *,
    identity_conflict_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        "slug": str(company.get("slug") or "").strip().lower(),
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
        "raw_payload": _json_safe(company),
    }
    if identity_conflict_evidence:
        metadata["identity_conflict_evidence"] = _json_safe(identity_conflict_evidence)
    return metadata


def _yc_company_id(company: dict[str, Any]) -> int:
    yc_company_id = _to_int(company.get("id")) or _to_int(company.get("objectID"))
    if yc_company_id is None:
        raise ValueError("YC company requires id or objectID")
    return yc_company_id


def _matching_neutral_company_id(connection: Any, company: dict[str, Any]) -> int | None:
    domain = primary_domain_for_website(sanitized_yc_company_website(company))
    normalized_name = normalize_company_name(str(company.get("name") or "").strip())
    if not domain or not normalized_name:
        return None
    candidates = list(
        connection.execute(
            select(companies_table).where(
                companies_table.c.primary_domain == domain,
                companies_table.c.normalized_name == normalized_name,
            )
        ).mappings()
    )
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    has_yc_source = connection.scalar(
        select(company_sources_table.c.id).where(
            company_sources_table.c.company_id == candidate["id"],
            company_sources_table.c.provider == "yc",
        )
    )
    return None if has_yc_source is not None else int(candidate["id"])


def _isolate_conflicting_neutral_website(
    connection: Any,
    company: dict[str, Any],
    *,
    exclude_company_id: int | None = None,
) -> None:
    domain = primary_domain_for_website(sanitized_yc_company_website(company))
    if not domain:
        return
    statement = select(companies_table.c.id, companies_table.c.name).where(
        companies_table.c.primary_domain == domain
    )
    if exclude_company_id is not None:
        statement = statement.where(companies_table.c.id != exclude_company_id)
    existing = list(connection.execute(statement).mappings())
    if not existing:
        return

    claimed_website = sanitized_yc_company_website(company)
    company["website"] = None
    company["_website_domain_conflict"] = True
    company["_identity_conflict_evidence"] = {
        "kind": "website_domain_conflict",
        "claimed_website": claimed_website,
        "claimed_domain": domain,
        "conflicting_companies": [
            {"company_id": int(row["id"]), "name": str(row["name"])} for row in existing
        ],
    }


def _available_company_slug(
    connection: Any,
    *,
    requested_slug: str,
    source_suffix: str,
) -> str:
    base = requested_slug.strip().lower() or source_suffix
    for candidate in (base, f"{base}-{source_suffix}"):
        if (
            connection.scalar(
                select(companies_table.c.id).where(companies_table.c.slug == candidate)
            )
            is None
        ):
            return candidate
    suffix = 2
    while True:
        candidate = f"{base}-{source_suffix}-{suffix}"
        if (
            connection.scalar(
                select(companies_table.c.id).where(companies_table.c.slug == candidate)
            )
            is None
        ):
            return candidate
        suffix += 1


def _company_row(
    company: dict[str, Any],
    *,
    slug: str,
    now: datetime,
    local_company_id: int | None = None,
    company_metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    name = str(company.get("name") or "").strip()
    if not name:
        raise ValueError("Company name is required")
    website = sanitized_yc_company_website(company)
    values: dict[str, Any] = {
        "name": name,
        "normalized_name": normalize_company_name(name),
        "slug": slug,
        "website": website,
        "primary_domain": primary_domain_for_website(website),
        "identity_state": "verified",
        "metadata": company_metadata or {},
        "created_at": created_at or now,
        "updated_at": now,
    }
    if local_company_id is not None:
        values["id"] = local_company_id
    return values


def _company_name_matches_domain(name: str, domain: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", normalize_company_name(name))
    if len(tokens) > 1 and tokens[-1] in {
        "incorporated",
        "corporation",
        "limited",
        "corp",
        "llc",
        "ltd",
        "inc",
    }:
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


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
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
