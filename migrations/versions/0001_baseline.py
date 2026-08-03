"""Create the small company-centric YC Radar schema.

This project is local and rebuildable, so this migration is intentionally a clean
baseline rather than a compatibility bridge from the earlier experimental schema.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("website", sa.Text()),
        sa.Column("primary_domain", sa.Text()),
        sa.Column(
            "identity_state",
            sa.Text(),
            nullable=False,
            server_default="verified",
        ),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "identity_state IN ('verified', 'provisional')",
            name="ck_companies_identity_state",
        ),
        sa.UniqueConstraint("slug", name="uq_companies_slug"),
    )
    op.create_index("ix_companies_normalized_name", "companies", ["normalized_name"])
    op.create_index("ix_companies_primary_domain", "companies", ["primary_domain"])
    op.create_index("ix_companies_identity_state", "companies", ["identity_state"])

    op.create_table(
        "company_sources",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("sync_mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sync_mode IN ('none', 'complete_snapshot', 'observation')",
            name="ck_company_sources_sync_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_company_sources_status",
        ),
        sa.UniqueConstraint(
            "provider",
            "external_id",
            name="uq_company_sources_provider_external_id",
        ),
    )
    op.create_index("ix_company_sources_company_id", "company_sources", ["company_id"])
    op.create_index("ix_company_sources_provider", "company_sources", ["provider"])
    op.create_index("ix_company_sources_status", "company_sources", ["status"])

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "company_source_id",
            sa.BigInteger(),
            sa.ForeignKey("company_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        sa.Column(
            "stats",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "details",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed')",
            name="ck_sync_runs_status",
        ),
        sa.UniqueConstraint(
            "company_source_id",
            "run_key",
            name="uq_sync_runs_source_run_key",
        ),
    )
    op.create_index("ix_sync_runs_company_source_id", "sync_runs", ["company_source_id"])
    op.create_index("ix_sync_runs_started_at", "sync_runs", ["started_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_source_id",
            sa.BigInteger(),
            sa.ForeignKey("company_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_job_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("posting_url", sa.Text()),
        sa.Column("apply_url", sa.Text()),
        sa.Column("description_text", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("department", sa.Text()),
        sa.Column("employment_type", sa.Text()),
        sa.Column(
            "structured_evidence",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "raw_payload",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "consecutive_complete_misses",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("source_published_at", sa.DateTime(timezone=True)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_seen_run_id",
            sa.BigInteger(),
            sa.ForeignKey("sync_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'closed')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint(
            "consecutive_complete_misses >= 0",
            name="ck_jobs_nonnegative_misses",
        ),
        sa.UniqueConstraint(
            "company_source_id",
            "external_job_id",
            name="uq_jobs_source_external_job_id",
        ),
    )
    op.create_index("ix_jobs_company_source_id", "jobs", ["company_source_id"])
    op.create_index("ix_jobs_company_status", "jobs", ["company_id", "status"])
    op.create_index("ix_jobs_last_changed_at", "jobs", ["last_changed_at"])
    op.create_index("ix_jobs_last_seen_run_id", "jobs", ["last_seen_run_id"])


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("sync_runs")
    op.drop_table("company_sources")
    op.drop_table("companies")
