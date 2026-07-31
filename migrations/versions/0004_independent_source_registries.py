"""Separate company-directory identities from ATS/feed source registrations."""

from alembic import op
import sqlalchemy as sa

revision = "0004_source_registries"
down_revision = "0003_source_neutral_companies"
branch_labels = None
depends_on = None

_NEUTRAL_PRIMARY_CAREER_PAGE_VIEW = """
    SELECT company_id, company_slug, company_name, website,
           career_page_url, page_type, discovery_source, confidence, http_status, evidence,
           checked_at
    FROM company_career_pages WHERE is_primary = true
"""

_LEGACY_PRIMARY_CAREER_PAGE_VIEW = """
    SELECT company_id, company_slug, company_name, website, yc_is_hiring, yc_job_count,
           career_page_url, page_type, discovery_source, confidence, http_status, evidence,
           checked_at
    FROM company_career_pages WHERE is_primary = true
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS company_primary_career_pages")
    with op.batch_alter_table("career_page_discovery_events") as batch:
        batch.drop_column("yc_job_count")
        batch.drop_column("yc_is_hiring")
    with op.batch_alter_table("company_career_pages") as batch:
        batch.drop_column("yc_job_count")
        batch.drop_column("yc_is_hiring")
    op.execute(f"CREATE VIEW company_primary_career_pages AS {_NEUTRAL_PRIMARY_CAREER_PAGE_VIEW}")

    # 0003 and the first registry implementation copied ATS board IDs into company_sources.
    # Those rows are not company-directory identities and are already represented by career_sources.
    op.execute(
        """
        DELETE FROM company_sources AS company_source
        USING career_sources AS career_source
        WHERE company_source.provider = career_source.provider
          AND company_source.external_company_id = career_source.external_source_id
          AND (
              company_source.provider IN ('greenhouse', 'ashby')
              OR company_source.raw_json ->> 'backfilled_from' IN (
                  'career_sources', 'career_source_registration'
              )
          )
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS company_primary_career_pages")
    with op.batch_alter_table("career_page_discovery_events") as batch:
        batch.add_column(
            sa.Column("yc_is_hiring", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch.add_column(
            sa.Column("yc_job_count", sa.Integer(), server_default="0", nullable=False)
        )
    with op.batch_alter_table("company_career_pages") as batch:
        batch.add_column(
            sa.Column("yc_is_hiring", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch.add_column(
            sa.Column("yc_job_count", sa.Integer(), server_default="0", nullable=False)
        )
    for table_name in ("career_page_discovery_events", "company_career_pages"):
        op.execute(
            f"""
            UPDATE {table_name} AS target
            SET yc_is_hiring = profile.is_hiring,
                yc_job_count = (
                    SELECT count(*)
                    FROM yc_job_postings AS job
                    WHERE job.company_id = profile.yc_company_id
                )
            FROM yc_company_profiles AS profile
            WHERE profile.company_id = target.company_id
            """
        )
        with op.batch_alter_table(table_name) as batch:
            batch.alter_column("yc_is_hiring", server_default=None)
            batch.alter_column("yc_job_count", server_default=None)
    op.execute(f"CREATE VIEW company_primary_career_pages AS {_LEGACY_PRIMARY_CAREER_PAGE_VIEW}")

    op.execute(
        """
        INSERT INTO company_sources (
            company_id, provider, external_company_id, source_url, raw_json,
            first_seen_at, last_seen_at, created_at, updated_at
        )
        SELECT company_id, provider, external_source_id, source_url,
               jsonb_build_object('provider', provider,
                                  'backfilled_from', 'career_sources'),
               created_at, updated_at, created_at, updated_at
        FROM career_sources
        ON CONFLICT (provider, external_company_id) DO NOTHING
        """
    )
