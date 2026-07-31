"""Split YC-specific company metadata from source-neutral company identities."""

import re
from urllib.parse import urlparse

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_source_neutral_companies"
down_revision = "0002_source_neutral_jobs"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB
_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)

_YC_COLUMNS = (
    "yc_url",
    "one_liner",
    "batch",
    "status",
    "stage",
    "team_size",
    "is_hiring",
    "all_locations",
    "regions",
    "industry",
    "subindustry",
    "industries",
    "tags",
    "prototype_score",
    "prototype_angle",
    "raw_json",
)


def upgrade() -> None:
    op.create_table(
        "yc_company_profiles",
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("yc_company_id", sa.Integer(), nullable=False),
        sa.Column("yc_url", sa.String(), nullable=False),
        sa.Column("one_liner", sa.Text()),
        sa.Column("batch", sa.String()),
        sa.Column("status", sa.String()),
        sa.Column("stage", sa.String()),
        sa.Column("team_size", sa.Integer()),
        sa.Column("is_hiring", sa.Boolean(), nullable=False),
        sa.Column("all_locations", sa.Text()),
        sa.Column("regions", JSONB, nullable=False),
        sa.Column("industry", sa.String()),
        sa.Column("subindustry", sa.String()),
        sa.Column("industries", JSONB, nullable=False),
        sa.Column("tags", JSONB, nullable=False),
        sa.Column("prototype_score", sa.Integer()),
        sa.Column("prototype_angle", sa.Text()),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("yc_company_id", name="uq_yc_company_profiles_yc_company_id"),
    )
    op.create_index(
        "ix_yc_company_profiles_yc_company_id",
        "yc_company_profiles",
        ["yc_company_id"],
        unique=True,
    )
    op.execute(
        """
        INSERT INTO yc_company_profiles (
            company_id, yc_company_id, yc_url, one_liner, batch, status, stage, team_size,
            is_hiring, all_locations, regions, industry, subindustry, industries, tags,
            prototype_score, prototype_angle, raw_json, created_at, updated_at
        )
        SELECT id,
               CASE
                   WHEN raw_json ? '_yc_radar_yc_company_id'
                        AND raw_json ->> '_yc_radar_yc_company_id' ~ '^[0-9]+$'
                   THEN (raw_json ->> '_yc_radar_yc_company_id')::integer
                   ELSE id
               END,
               yc_url, one_liner, batch, status, stage, team_size, is_hiring,
               all_locations, regions, industry, subindustry, industries, tags,
               prototype_score, prototype_angle,
               raw_json - '_yc_radar_yc_company_id', created_at, updated_at
        FROM companies
        """
    )
    op.add_column("companies", sa.Column("normalized_name", sa.String(), nullable=True))
    op.add_column("companies", sa.Column("primary_domain", sa.String(), nullable=True))
    connection = op.get_bind()
    companies = connection.execute(
        sa.text("SELECT id, name, website FROM companies")
    ).mappings().all()
    for company in companies:
        connection.execute(
            sa.text(
                """
                UPDATE companies
                SET normalized_name = :normalized_name,
                    primary_domain = :primary_domain
                WHERE id = :company_id
                """
            ),
            {
                "company_id": company["id"],
                "normalized_name": _normalize_company_name(company["name"]),
                "primary_domain": _primary_domain(company["website"]),
            },
        )
    op.alter_column("companies", "normalized_name", nullable=False)
    op.create_index("ix_companies_normalized_name", "companies", ["normalized_name"])
    op.create_index("ix_companies_primary_domain", "companies", ["primary_domain"])
    with op.batch_alter_table("companies") as batch:
        for column in _YC_COLUMNS:
            batch.drop_column(column)
    op.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('companies', 'id'),
            COALESCE((SELECT MAX(id) FROM companies), 1),
            (SELECT MAX(id) IS NOT NULL FROM companies)
        )
        """
    )


def _normalize_company_name(value: str) -> str:
    return " ".join(value.lower().split())


def _primary_domain(website: str | None) -> str | None:
    if not website:
        return None
    try:
        host = (urlparse(website).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    domain = host.removeprefix("www.")
    return domain if _DOMAIN_PATTERN.fullmatch(domain) else None


def downgrade() -> None:
    has_non_yc_companies = op.get_bind().execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM companies AS company
                LEFT JOIN yc_company_profiles AS profile ON profile.company_id = company.id
                WHERE profile.company_id IS NULL
            )
            """
        )
    ).scalar_one()
    if has_non_yc_companies:
        raise RuntimeError(
            "Cannot downgrade 0003_source_neutral_companies while non-YC companies exist; "
            "the 0002 schema cannot represent them without fabricating YC metadata."
        )
    has_independent_local_ids = op.get_bind().execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM yc_company_profiles
                WHERE company_id <> yc_company_id
            )
            """
        )
    ).scalar_one()
    if has_independent_local_ids:
        raise RuntimeError(
            "Cannot downgrade 0003_source_neutral_companies when YC external IDs differ from "
            "local company IDs; the 0002 schema cannot preserve that identity mapping."
        )
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("yc_url", sa.String(), nullable=True))
        batch.add_column(sa.Column("one_liner", sa.Text()))
        batch.add_column(sa.Column("batch", sa.String()))
        batch.add_column(sa.Column("status", sa.String()))
        batch.add_column(sa.Column("stage", sa.String()))
        batch.add_column(sa.Column("team_size", sa.Integer()))
        batch.add_column(sa.Column("is_hiring", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("all_locations", sa.Text()))
        batch.add_column(sa.Column("regions", JSONB, nullable=True))
        batch.add_column(sa.Column("industry", sa.String()))
        batch.add_column(sa.Column("subindustry", sa.String()))
        batch.add_column(sa.Column("industries", JSONB, nullable=True))
        batch.add_column(sa.Column("tags", JSONB, nullable=True))
        batch.add_column(sa.Column("prototype_score", sa.Integer()))
        batch.add_column(sa.Column("prototype_angle", sa.Text()))
        batch.add_column(sa.Column("raw_json", JSONB, nullable=True))
    op.execute(
        """
        UPDATE companies AS company
        SET yc_url = profile.yc_url,
            one_liner = profile.one_liner,
            batch = profile.batch,
            status = profile.status,
            stage = profile.stage,
            team_size = profile.team_size,
            is_hiring = profile.is_hiring,
            all_locations = profile.all_locations,
            regions = profile.regions,
            industry = profile.industry,
            subindustry = profile.subindustry,
            industries = profile.industries,
            tags = profile.tags,
            prototype_score = profile.prototype_score,
            prototype_angle = profile.prototype_angle,
            raw_json = profile.raw_json
        FROM yc_company_profiles AS profile
        WHERE profile.company_id = company.id
        """
    )
    with op.batch_alter_table("companies") as batch:
        batch.alter_column("yc_url", nullable=False)
        batch.alter_column("is_hiring", nullable=False)
        batch.alter_column("regions", nullable=False)
        batch.alter_column("industries", nullable=False)
        batch.alter_column("tags", nullable=False)
        batch.alter_column("raw_json", nullable=False)
    op.drop_index("ix_companies_primary_domain", table_name="companies")
    op.drop_index("ix_companies_normalized_name", table_name="companies")
    with op.batch_alter_table("companies") as batch:
        batch.drop_column("primary_domain")
        batch.drop_column("normalized_name")
    op.drop_table("yc_company_profiles")
