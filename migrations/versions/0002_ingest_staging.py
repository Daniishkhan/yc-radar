"""Add durable ingest staging and remove redundant job company ownership.

Revision ID: 0002_ingest_staging
Revises: 0001_core
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_ingest_staging"
down_revision = "0001_core"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB
INGEST_SCHEMA = "ingest"


def upgrade() -> None:
    # The baseline stored company ownership twice.  Lock both tables so a writer
    # cannot introduce drift between this check and the column removal, then fail
    # closed if an older/manual write left the two ownership paths inconsistent.
    op.execute(sa.text("LOCK TABLE jobs, company_sources IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            """
            DO $yc_radar$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM jobs
                    JOIN company_sources
                      ON company_sources.id = jobs.company_source_id
                    WHERE jobs.company_id IS DISTINCT FROM company_sources.company_id
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'jobs.company_id disagrees with company_sources.company_id; '
                                  'refusing to remove redundant job ownership';
                END IF;
            END
            $yc_radar$;
            """
        )
    )
    op.drop_index("ix_jobs_company_status", table_name="jobs")
    op.drop_column("jobs", "company_id")
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{INGEST_SCHEMA}"'))
    _create_runs()
    _create_url_work_items()
    _create_raw_observations()
    _create_job_candidates()


def _create_runs() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_key", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("normalizer_version", sa.Text(), nullable=False),
        sa.Column("input_uri", sa.Text()),
        sa.Column("input_sha256", sa.Text()),
        sa.Column(
            "cursor",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "stats",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed')",
            name="ck_ingest_runs_status",
        ),
        sa.CheckConstraint(
            "char_length(run_key) BETWEEN 1 AND 512",
            name="ck_ingest_runs_run_key_length",
        ),
        sa.CheckConstraint(
            "char_length(source) BETWEEN 1 AND 128",
            name="ck_ingest_runs_source_length",
        ),
        sa.CheckConstraint(
            "char_length(parser_version) BETWEEN 1 AND 128",
            name="ck_ingest_runs_parser_version_length",
        ),
        sa.CheckConstraint(
            "char_length(normalizer_version) BETWEEN 1 AND 128",
            name="ck_ingest_runs_normalizer_version_length",
        ),
        sa.CheckConstraint(
            "input_uri IS NULL OR char_length(input_uri) BETWEEN 1 AND 8192",
            name="ck_ingest_runs_input_uri_length",
        ),
        sa.CheckConstraint(
            "input_sha256 IS NULL OR input_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_ingest_runs_input_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(cursor) = 'object' AND pg_column_size(cursor) <= 262144",
            name="ck_ingest_runs_cursor",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(stats) = 'object' AND pg_column_size(stats) <= 262144",
            name="ck_ingest_runs_stats",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND completed_at IS NULL) OR "
            "(status <> 'running' AND completed_at IS NOT NULL)",
            name="ck_ingest_runs_completion",
        ),
        sa.UniqueConstraint(
            "source",
            "run_key",
            name="uq_ingest_runs_source_run_key",
        ),
        schema=INGEST_SCHEMA,
    )
    op.create_index(
        "ix_ingest_runs_started_at",
        "runs",
        ["started_at"],
        schema=INGEST_SCHEMA,
    )
    op.create_index(
        "ix_ingest_runs_running",
        "runs",
        ["started_at", "id"],
        schema=INGEST_SCHEMA,
        postgresql_where=sa.text("status = 'running'"),
    )


def _create_url_work_items() -> None:
    op.create_table(
        "url_work_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "ingest.runs.id",
                name="fk_ingest_url_work_items_run_id",
                ondelete="SET NULL",
            ),
        ),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False, server_default="fetch"),
        sa.Column("state", sa.Text(), nullable=False, server_default="ready"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_token", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("artifact_uri", sa.Text()),
        sa.Column("http_status", sa.Integer()),
        sa.Column("content_type", sa.Text()),
        sa.Column("content_hash", sa.Text()),
        sa.Column(
            "result",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "last_error",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("normalizer_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stage IN ('fetch', 'parse', 'enrich', 'promote', 'done')",
            name="ck_ingest_url_work_items_stage",
        ),
        sa.CheckConstraint(
            "state IN "
            "('ready', 'leased', 'retry', 'verified', 'promoted', "
            "'quarantined', 'dead')",
            name="ck_ingest_url_work_items_state",
        ),
        sa.CheckConstraint(
            "priority BETWEEN -1000000 AND 1000000",
            name="ck_ingest_url_work_items_priority",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts BETWEEN 1 AND 100 "
            "AND attempt_count <= max_attempts",
            name="ck_ingest_url_work_items_attempts",
        ),
        sa.CheckConstraint(
            "(state = 'leased' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(state <> 'leased' AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_ingest_url_work_items_lease",
        ),
        sa.CheckConstraint(
            "char_length(normalized_url) BETWEEN 1 AND 2048",
            name="ck_ingest_url_work_items_url_length",
        ),
        sa.CheckConstraint(
            "char_length(host) BETWEEN 1 AND 253",
            name="ck_ingest_url_work_items_host_length",
        ),
        sa.CheckConstraint(
            "lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 256",
            name="ck_ingest_url_work_items_lease_owner_length",
        ),
        sa.CheckConstraint(
            "lease_token IS NULL OR char_length(lease_token) BETWEEN 1 AND 512",
            name="ck_ingest_url_work_items_lease_token_length",
        ),
        sa.CheckConstraint(
            "artifact_uri IS NULL OR char_length(artifact_uri) BETWEEN 1 AND 8192",
            name="ck_ingest_url_work_items_artifact_uri_length",
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_ingest_url_work_items_http_status",
        ),
        sa.CheckConstraint(
            "content_type IS NULL OR char_length(content_type) BETWEEN 1 AND 255",
            name="ck_ingest_url_work_items_content_type_length",
        ),
        sa.CheckConstraint(
            "content_hash IS NULL OR char_length(content_hash) BETWEEN 1 AND 256",
            name="ck_ingest_url_work_items_content_hash_length",
        ),
        sa.CheckConstraint(
            "char_length(parser_version) BETWEEN 1 AND 128",
            name="ck_ingest_url_work_items_parser_version_length",
        ),
        sa.CheckConstraint(
            "char_length(normalizer_version) BETWEEN 1 AND 128",
            name="ck_ingest_url_work_items_normalizer_version_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result) = 'object' AND pg_column_size(result) <= 262144",
            name="ck_ingest_url_work_items_result",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(last_error) = 'object' AND pg_column_size(last_error) <= 262144",
            name="ck_ingest_url_work_items_last_error",
        ),
        sa.UniqueConstraint(
            "normalized_url",
            "parser_version",
            "normalizer_version",
            name="uq_ingest_url_work_items_url_versions",
        ),
        schema=INGEST_SCHEMA,
    )
    op.create_index(
        "ix_ingest_url_work_items_host",
        "url_work_items",
        ["host"],
        schema=INGEST_SCHEMA,
    )
    op.create_index(
        "ix_ingest_url_work_items_queue",
        "url_work_items",
        [sa.text("priority DESC"), "available_at", "id"],
        schema=INGEST_SCHEMA,
        postgresql_where=sa.text("state IN ('ready', 'retry') AND stage <> 'done'"),
    )
    op.create_index(
        "ix_ingest_url_work_items_lease",
        "url_work_items",
        ["lease_expires_at", "id"],
        schema=INGEST_SCHEMA,
        postgresql_where=sa.text("state = 'leased'"),
    )


def _create_raw_observations() -> None:
    op.create_table(
        "raw_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "ingest.runs.id",
                name="fk_ingest_raw_observations_run_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "url_work_item_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "ingest.url_work_items.id",
                name="fk_ingest_raw_observations_url_work_item_id",
                ondelete="SET NULL",
            ),
        ),
        sa.Column("observation_key", sa.Text(), nullable=False),
        sa.Column("observed_url", sa.Text()),
        sa.Column(
            "payload",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(observation_key) BETWEEN 1 AND 512",
            name="ck_ingest_raw_observations_key_length",
        ),
        sa.CheckConstraint(
            "observed_url IS NULL OR char_length(observed_url) BETWEEN 1 AND 8192",
            name="ck_ingest_raw_observations_url_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND pg_column_size(payload) <= 1048576",
            name="ck_ingest_raw_observations_payload",
        ),
        sa.UniqueConstraint(
            "run_id",
            "observation_key",
            name="uq_ingest_raw_observations_run_key",
        ),
        schema=INGEST_SCHEMA,
    )
    op.create_index(
        "ix_ingest_raw_observations_url_work_item_id",
        "raw_observations",
        ["url_work_item_id"],
        schema=INGEST_SCHEMA,
        postgresql_where=sa.text("url_work_item_id IS NOT NULL"),
    )
    op.create_index(
        "ix_ingest_raw_observations_observed_at",
        "raw_observations",
        ["observed_at"],
        schema=INGEST_SCHEMA,
    )


def _create_job_candidates() -> None:
    op.create_table(
        "job_candidates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "ingest.runs.id",
                name="fk_ingest_job_candidates_run_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "raw_observation_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "ingest.raw_observations.id",
                name="fk_ingest_job_candidates_raw_observation_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "work_item_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "ingest.url_work_items.id",
                name="fk_ingest_job_candidates_work_item_id",
                ondelete="SET NULL",
            ),
        ),
        sa.Column("candidate_key", sa.Text(), nullable=False),
        sa.Column(
            "company_source_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "company_sources.id",
                name="fk_ingest_job_candidates_company_source_id",
                ondelete="RESTRICT",
            ),
        ),
        sa.Column("provider", sa.Text()),
        sa.Column("external_source_id", sa.Text()),
        sa.Column("external_job_id", sa.Text()),
        sa.Column("snapshot_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("title", sa.Text()),
        sa.Column("posting_url", sa.Text()),
        sa.Column("apply_url", sa.Text()),
        sa.Column("description_text", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("department", sa.Text()),
        sa.Column("employment_type", sa.Text()),
        sa.Column("content_hash", sa.Text()),
        sa.Column("source_published_at", sa.DateTime(timezone=True)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "field_provenance",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "quality_flags",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "payload",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="normalized"),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("normalizer_version", sa.Text(), nullable=False),
        sa.Column(
            "error",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "promoted_job_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "jobs.id",
                name="fk_ingest_job_candidates_promoted_job_id",
                ondelete="SET NULL",
            ),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('normalized', 'ready', 'quarantined', 'promoted', 'rejected')",
            name="ck_ingest_job_candidates_status",
        ),
        sa.CheckConstraint(
            "raw_observation_id IS NOT NULL",
            name="ck_ingest_job_candidates_lineage",
        ),
        sa.CheckConstraint(
            "char_length(candidate_key) BETWEEN 1 AND 512",
            name="ck_ingest_job_candidates_key_length",
        ),
        sa.CheckConstraint(
            "provider IS NULL OR char_length(provider) BETWEEN 1 AND 128",
            name="ck_ingest_job_candidates_provider_length",
        ),
        sa.CheckConstraint(
            "external_source_id IS NULL OR char_length(external_source_id) BETWEEN 1 AND 512",
            name="ck_ingest_job_candidates_external_source_id_length",
        ),
        sa.CheckConstraint(
            "external_job_id IS NULL OR char_length(external_job_id) BETWEEN 1 AND 512",
            name="ck_ingest_job_candidates_external_job_id_length",
        ),
        sa.CheckConstraint(
            "title IS NULL OR char_length(title) BETWEEN 1 AND 1000",
            name="ck_ingest_job_candidates_title_length",
        ),
        sa.CheckConstraint(
            "posting_url IS NULL OR char_length(posting_url) BETWEEN 1 AND 8192",
            name="ck_ingest_job_candidates_posting_url_length",
        ),
        sa.CheckConstraint(
            "apply_url IS NULL OR char_length(apply_url) BETWEEN 1 AND 8192",
            name="ck_ingest_job_candidates_apply_url_length",
        ),
        sa.CheckConstraint(
            "description_text IS NULL OR octet_length(description_text) <= 1048576",
            name="ck_ingest_job_candidates_description_size",
        ),
        sa.CheckConstraint(
            "location IS NULL OR char_length(location) BETWEEN 1 AND 2000",
            name="ck_ingest_job_candidates_location_length",
        ),
        sa.CheckConstraint(
            "department IS NULL OR char_length(department) BETWEEN 1 AND 1000",
            name="ck_ingest_job_candidates_department_length",
        ),
        sa.CheckConstraint(
            "employment_type IS NULL OR char_length(employment_type) BETWEEN 1 AND 512",
            name="ck_ingest_job_candidates_employment_type_length",
        ),
        sa.CheckConstraint(
            "content_hash IS NULL OR char_length(content_hash) BETWEEN 1 AND 256",
            name="ck_ingest_job_candidates_content_hash_length",
        ),
        sa.CheckConstraint(
            "char_length(parser_version) BETWEEN 1 AND 128",
            name="ck_ingest_job_candidates_parser_version_length",
        ),
        sa.CheckConstraint(
            "char_length(normalizer_version) BETWEEN 1 AND 128",
            name="ck_ingest_job_candidates_normalizer_version_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(field_provenance) = 'object' "
            "AND pg_column_size(field_provenance) <= 262144",
            name="ck_ingest_job_candidates_field_provenance",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(quality_flags) = 'array' "
            "AND pg_column_size(quality_flags) <= 262144",
            name="ck_ingest_job_candidates_quality_flags",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND pg_column_size(payload) <= 1048576",
            name="ck_ingest_job_candidates_payload",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(error) = 'object' AND pg_column_size(error) <= 262144",
            name="ck_ingest_job_candidates_error",
        ),
        sa.CheckConstraint(
            "status NOT IN ('ready', 'promoted') OR "
            "(provider IS NOT NULL AND btrim(provider) <> '' "
            "AND external_source_id IS NOT NULL AND btrim(external_source_id) <> '' "
            "AND external_job_id IS NOT NULL AND btrim(external_job_id) <> '' "
            "AND title IS NOT NULL AND btrim(title) <> '')",
            name="ck_ingest_job_candidates_ready_fields",
        ),
        sa.CheckConstraint(
            "status <> 'promoted' OR company_source_id IS NOT NULL",
            name="ck_ingest_job_candidates_promoted_source",
        ),
        sa.CheckConstraint(
            "promoted_job_id IS NULL OR status = 'promoted'",
            name="ck_ingest_job_candidates_promoted_job",
        ),
        sa.UniqueConstraint(
            "run_id",
            "candidate_key",
            name="uq_ingest_job_candidates_run_key",
        ),
        schema=INGEST_SCHEMA,
    )
    op.create_index(
        "ix_ingest_job_candidates_raw_observation_id",
        "job_candidates",
        ["raw_observation_id"],
        schema=INGEST_SCHEMA,
        postgresql_where=sa.text("raw_observation_id IS NOT NULL"),
    )
    op.create_index(
        "ix_ingest_job_candidates_work_item_id",
        "job_candidates",
        ["work_item_id"],
        schema=INGEST_SCHEMA,
        postgresql_where=sa.text("work_item_id IS NOT NULL"),
    )
    op.create_index(
        "ix_ingest_job_candidates_ready",
        "job_candidates",
        ["company_source_id", "id"],
        schema=INGEST_SCHEMA,
        postgresql_where=sa.text("status = 'ready'"),
    )
    op.create_index(
        "ix_ingest_job_candidates_promoted_job_id",
        "job_candidates",
        ["promoted_job_id"],
        schema=INGEST_SCHEMA,
        postgresql_where=sa.text("promoted_job_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("job_candidates", schema=INGEST_SCHEMA)
    op.drop_table("raw_observations", schema=INGEST_SCHEMA)
    op.drop_table("url_work_items", schema=INGEST_SCHEMA)
    op.drop_table("runs", schema=INGEST_SCHEMA)
    op.execute(sa.text(f'DROP SCHEMA IF EXISTS "{INGEST_SCHEMA}"'))

    op.drop_index("ix_jobs_status", table_name="jobs")
    op.add_column("jobs", sa.Column("company_id", sa.BigInteger(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE jobs SET company_id = company_sources.company_id "
            "FROM company_sources "
            "WHERE company_sources.id = jobs.company_source_id"
        )
    )
    op.alter_column("jobs", "company_id", nullable=False)
    op.create_foreign_key(
        "jobs_company_id_fkey",
        "jobs",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_jobs_company_status", "jobs", ["company_id", "status"])
