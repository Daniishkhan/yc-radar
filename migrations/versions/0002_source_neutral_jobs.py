"""Add source-neutral career sources, canonical jobs, and lifecycle history."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_source_neutral_jobs"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB


def upgrade() -> None:
    op.create_table(
        "company_sources",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("external_company_id", sa.String(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "external_company_id", name="uq_company_source_provider_external"),
    )
    op.create_index("ix_company_sources_company_id", "company_sources", ["company_id"])
    op.create_index("ix_company_sources_provider", "company_sources", ["provider"])
    op.execute(
        """
        INSERT INTO company_sources (
            company_id, provider, external_company_id, source_url, raw_json,
            first_seen_at, last_seen_at, created_at, updated_at
        )
        SELECT id, 'yc', id::text, yc_url, jsonb_build_object('provider', 'yc'),
               now(), now(), now(), now()
        FROM companies
        """
    )
    op.create_table(
        "career_sources",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("external_source_id", sa.String(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("discovered_from_url", sa.Text()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("last_sync_status", sa.String()),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_career_sources_status"),
        sa.UniqueConstraint("provider", "external_source_id", name="uq_career_source_provider_external"),
    )
    op.create_index("ix_career_sources_company_id", "career_sources", ["company_id"])
    op.create_index("ix_career_sources_provider", "career_sources", ["provider"])
    op.create_index("ix_career_sources_status", "career_sources", ["status"])
    op.create_table(
        "source_sync_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("career_source_id", sa.BigInteger(), sa.ForeignKey("career_sources.id"), nullable=False),
        sa.Column("run_key", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("adapter_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("is_complete_scan", sa.Boolean(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("jobs_fetched", sa.Integer(), nullable=False),
        sa.Column("jobs_added", sa.Integer(), nullable=False),
        sa.Column("jobs_updated", sa.Integer(), nullable=False),
        sa.Column("jobs_unchanged", sa.Integer(), nullable=False),
        sa.Column("jobs_missed", sa.Integer(), nullable=False),
        sa.Column("jobs_closed", sa.Integer(), nullable=False),
        sa.Column("jobs_reactivated", sa.Integer(), nullable=False),
        sa.Column("errors_count", sa.Integer(), nullable=False),
        sa.Column("errors", JSONB, nullable=False),
        sa.Column("request_metadata", JSONB, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('running', 'completed', 'partial', 'failed')", name="ck_source_sync_runs_status"),
        sa.CheckConstraint("jobs_fetched >= 0", name="ck_source_sync_runs_jobs_fetched"),
        sa.UniqueConstraint("career_source_id", "run_key", name="uq_source_sync_run_key"),
    )
    op.create_index("ix_source_sync_runs_career_source_id", "source_sync_runs", ["career_source_id"])
    op.create_table(
        "job_postings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("career_source_id", sa.BigInteger(), sa.ForeignKey("career_sources.id"), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("external_job_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("posting_url", sa.Text()),
        sa.Column("apply_url", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("department", sa.Text()),
        sa.Column("employment_type", sa.Text()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("consecutive_complete_misses", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("current_version_id", sa.BigInteger()),
        sa.Column("source_published_at", sa.DateTime(timezone=True)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'closed')", name="ck_job_postings_status"),
        sa.CheckConstraint("consecutive_complete_misses >= 0", name="ck_job_postings_misses"),
        sa.UniqueConstraint("provider", "career_source_id", "external_job_id", name="uq_job_posting_identity"),
    )
    op.create_index("ix_job_postings_career_source_id", "job_postings", ["career_source_id"])
    op.create_index("ix_job_postings_company_id", "job_postings", ["company_id"])
    op.create_index("ix_job_postings_content_hash", "job_postings", ["content_hash"])
    op.create_index("ix_job_postings_last_changed_at", "job_postings", ["last_changed_at"])
    op.create_index("ix_job_postings_company_active", "job_postings", ["company_id", "status"])
    op.create_table(
        "job_posting_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_posting_id", sa.BigInteger(), sa.ForeignKey("job_postings.id"), nullable=False),
        sa.Column("source_sync_run_id", sa.BigInteger(), sa.ForeignKey("source_sync_runs.id"), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description_html", sa.Text()),
        sa.Column("description_text", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("department", sa.Text()),
        sa.Column("employment_type", sa.Text()),
        sa.Column("posting_url", sa.Text()),
        sa.Column("apply_url", sa.Text()),
        sa.Column("source_published_at", sa.DateTime(timezone=True)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("raw_payload", JSONB, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_posting_id", "source_sync_run_id", name="uq_job_posting_version_run"),
    )
    op.create_index("ix_job_posting_versions_job_posting_id", "job_posting_versions", ["job_posting_id"])
    op.create_index("ix_job_posting_versions_source_sync_run_id", "job_posting_versions", ["source_sync_run_id"])
    op.create_index("ix_job_posting_versions_content_hash", "job_posting_versions", ["content_hash"])
    op.create_foreign_key("fk_job_postings_current_version", "job_postings", "job_posting_versions", ["current_version_id"], ["id"])
    op.create_table(
        "job_posting_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_posting_id", sa.BigInteger(), sa.ForeignKey("job_postings.id"), nullable=False),
        sa.Column("source_sync_run_id", sa.BigInteger(), sa.ForeignKey("source_sync_runs.id"), nullable=False),
        sa.Column("observation_kind", sa.String(), nullable=False),
        sa.Column("status_before", sa.String(), nullable=False),
        sa.Column("status_after", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String()),
        sa.Column("job_posting_version_id", sa.BigInteger(), sa.ForeignKey("job_posting_versions.id")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", JSONB, nullable=False),
        sa.CheckConstraint("observation_kind IN ('seen', 'missed')", name="ck_job_observation_kind"),
        sa.CheckConstraint("status_before IN ('active', 'closed')", name="ck_job_observation_status_before"),
        sa.CheckConstraint("status_after IN ('active', 'closed')", name="ck_job_observation_status_after"),
        sa.UniqueConstraint("source_sync_run_id", "job_posting_id", name="uq_job_observation_run_job"),
    )
    op.create_index("ix_job_posting_observations_job_posting_id", "job_posting_observations", ["job_posting_id"])
    op.create_index("ix_job_posting_observations_source_sync_run_id", "job_posting_observations", ["source_sync_run_id"])


def downgrade() -> None:
    op.drop_table("job_posting_observations")
    op.drop_constraint("fk_job_postings_current_version", "job_postings", type_="foreignkey")
    op.drop_table("job_posting_versions")
    op.drop_table("job_postings")
    op.drop_table("source_sync_runs")
    op.drop_table("career_sources")
    op.drop_table("company_sources")
