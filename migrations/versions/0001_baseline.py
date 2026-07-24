"""Create the legacy YC Radar schema as the migration baseline."""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB
TSVECTOR = postgresql.TSVECTOR


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("yc_url", sa.String(), nullable=False),
        sa.Column("website", sa.String()),
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
    )
    op.create_index("ix_companies_slug", "companies", ["slug"], unique=True)
    op.create_table(
        "yc_job_postings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("company_yc_url", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("absolute_url", sa.String(), nullable=False),
        sa.Column("apply_url", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("type", sa.String()),
        sa.Column("role", sa.String()),
        sa.Column("role_specific_type", sa.String()),
        sa.Column("pretty_role", sa.String()),
        sa.Column("salary_range", sa.String()),
        sa.Column("equity_range", sa.String()),
        sa.Column("min_experience", sa.String()),
        sa.Column("min_school_year", sa.String()),
        sa.Column("visa", sa.String()),
        sa.Column("skills", JSONB, nullable=False),
        sa.Column("is_incomplete", sa.Boolean(), nullable=False),
        sa.Column("created_at_text", sa.String()),
        sa.Column("last_active_text", sa.String()),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_yc_job_postings_company_id", "yc_job_postings", ["company_id"])
    op.create_index("ix_yc_job_postings_company_slug", "yc_job_postings", ["company_slug"])
    op.create_table(
        "career_page_discovery_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("website", sa.String()),
        sa.Column("yc_is_hiring", sa.Boolean(), nullable=False),
        sa.Column("yc_job_count", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("page_type", sa.String(), nullable=False),
        sa.Column("discovery_source", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("evidence", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_career_page_discovery_events_company_id", "career_page_discovery_events", ["company_id"])
    op.create_index("ix_career_page_discovery_events_company_slug", "career_page_discovery_events", ["company_slug"])
    op.create_table(
        "company_career_pages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("website", sa.String()),
        sa.Column("yc_is_hiring", sa.Boolean(), nullable=False),
        sa.Column("yc_job_count", sa.Integer(), nullable=False),
        sa.Column("career_page_url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("page_type", sa.String(), nullable=False),
        sa.Column("discovery_source", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("evidence", sa.Text()),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("observed_source_count", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_slug", "normalized_url", name="uq_company_career_page_url"),
    )
    op.create_index("ix_company_career_pages_company_id", "company_career_pages", ["company_id"])
    op.create_index("ix_company_career_pages_company_slug", "company_career_pages", ["company_slug"])
    op.create_table(
        "discovered_urls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("website", sa.String()),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("url_key", sa.String(), nullable=False),
        sa.Column("url_kind", sa.String(), nullable=False),
        sa.Column("discovery_sources", JSONB, nullable=False),
        sa.Column("evidence_samples", JSONB, nullable=False),
        sa.Column("source_event_count", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("fetch_priority", sa.Float(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_slug", "url_key", name="uq_discovered_url_company_key"),
    )
    op.create_index("ix_discovered_urls_company_id", "discovered_urls", ["company_id"])
    op.create_index("ix_discovered_urls_company_slug", "discovered_urls", ["company_slug"])
    op.create_index("ix_discovered_urls_url_kind", "discovered_urls", ["url_kind"])
    op.create_index("ix_discovered_urls_priority", "discovered_urls", ["fetch_priority"])
    op.create_table(
        "career_page_discovery_statuses",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("website", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("discovery_event_count", sa.Integer(), nullable=False),
        sa.Column("career_page_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_career_page_discovery_statuses_company_id", "career_page_discovery_statuses", ["company_id"])
    op.create_index("ix_career_page_discovery_statuses_company_slug", "career_page_discovery_statuses", ["company_slug"], unique=True)
    op.create_index("ix_career_page_discovery_statuses_status", "career_page_discovery_statuses", ["status"])
    op.create_table(
        "source_documents",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("discovered_url_id", sa.BigInteger(), sa.ForeignKey("discovered_urls.id", ondelete="SET NULL")),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("normalized_url", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("raw_text", sa.Text()),
        sa.Column("clean_text", sa.Text()),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("search_vector", TSVECTOR, sa.Computed("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(clean_text, ''))", persisted=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_type", "source_key", name="uq_source_document_source_key"),
    )
    op.create_index("ix_source_documents_company_id", "source_documents", ["company_id"])
    op.create_index("ix_source_documents_company_slug", "source_documents", ["company_slug"])
    op.create_index("ix_source_documents_source_type", "source_documents", ["source_type"])
    op.create_index("ix_source_documents_content_hash", "source_documents", ["content_hash"])
    op.create_index("ix_source_documents_search_vector", "source_documents", ["search_vector"], postgresql_using="gin")
    op.create_table(
        "page_classifications",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_document_id", sa.BigInteger(), sa.ForeignKey("source_documents.id", ondelete="CASCADE")),
        sa.Column("discovered_url_id", sa.BigInteger(), sa.ForeignKey("discovered_urls.id", ondelete="SET NULL")),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("page_kind", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("parser_name", sa.String(), nullable=False),
        sa.Column("parser_version", sa.String(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("job_title", sa.Text()),
        sa.Column("role_titles", JSONB, nullable=False),
        sa.Column("job_count", sa.Integer(), nullable=False),
        sa.Column("evidence", JSONB, nullable=False),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_document_id", "parser_name", name="uq_page_classification_document_parser"),
    )
    op.create_index("ix_page_classifications_company_id", "page_classifications", ["company_id"])
    op.create_index("ix_page_classifications_company_slug", "page_classifications", ["company_slug"])
    op.create_index("ix_page_classifications_page_kind", "page_classifications", ["page_kind"])
    op.create_table(
        "external_job_postings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("source_document_id", sa.BigInteger(), sa.ForeignKey("source_documents.id", ondelete="SET NULL")),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_job_id", sa.String()),
        sa.Column("posting_url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("apply_url", sa.Text()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description_text", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("employment_type", sa.String()),
        sa.Column("department", sa.String()),
        sa.Column("seniority", sa.String()),
        sa.Column("salary_range", sa.Text()),
        sa.Column("equity_range", sa.Text()),
        sa.Column("visa", sa.Text()),
        sa.Column("remote_policy", sa.Text()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("role_fit", sa.String(), nullable=False),
        sa.Column("extraction_confidence", sa.Float(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "normalized_url", name="uq_external_job_source_url"),
    )
    op.create_index("ix_external_job_postings_company_id", "external_job_postings", ["company_id"])
    op.create_index("ix_external_job_postings_company_slug", "external_job_postings", ["company_slug"])
    op.create_index("ix_external_job_postings_source", "external_job_postings", ["source"])
    op.create_index("ix_external_job_postings_role_fit", "external_job_postings", ["role_fit"])
    op.create_table(
        "job_extraction_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_document_id", sa.BigInteger(), sa.ForeignKey("source_documents.id", ondelete="CASCADE")),
        sa.Column("parser_name", sa.String(), nullable=False),
        sa.Column("model", sa.String()),
        sa.Column("prompt_version", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("extracted_jobs_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_document_id", sa.BigInteger(), sa.ForeignKey("source_documents.id", ondelete="CASCADE")),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("token_count", sa.Integer()),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("search_vector", TSVECTOR, sa.Computed("to_tsvector('english', coalesce(chunk_text, ''))", persisted=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_document_id", "chunk_index", name="uq_document_chunk_source_index"),
    )
    op.create_index("ix_document_chunks_company_id", "document_chunks", ["company_id"])
    op.create_index("ix_document_chunks_company_slug", "document_chunks", ["company_slug"])
    op.create_index("ix_document_chunks_source_type", "document_chunks", ["source_type"])
    op.create_index("ix_document_chunks_content_hash", "document_chunks", ["content_hash"])
    op.create_index("ix_document_chunks_search_vector", "document_chunks", ["search_vector"], postgresql_using="gin")
    op.create_table(
        "document_embeddings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chunk_id", sa.BigInteger(), sa.ForeignKey("document_chunks.id", ondelete="CASCADE")),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chunk_id", "embedding_model", "embedding_dimensions", name="uq_document_embedding_chunk_model"),
    )
    op.create_index("ix_document_embeddings_company_id", "document_embeddings", ["company_id"])
    op.create_index("ix_document_embeddings_company_slug", "document_embeddings", ["company_slug"])
    op.create_index("ix_document_embeddings_embedding_model", "document_embeddings", ["embedding_model"])
    op.create_index("ix_document_embeddings_embedding_hnsw", "document_embeddings", ["embedding"], postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"})
    op.create_table(
        "job_role_signals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer()),
        sa.Column("company_slug", sa.String(), nullable=False),
        sa.Column("yc_job_id", sa.Integer(), sa.ForeignKey("yc_job_postings.id", ondelete="CASCADE")),
        sa.Column("external_job_id", sa.BigInteger(), sa.ForeignKey("external_job_postings.id", ondelete="CASCADE")),
        sa.Column("signal_type", sa.String(), nullable=False),
        sa.Column("signal_value", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.Text()),
        sa.Column("raw_json", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_job_role_signals_company_id", "job_role_signals", ["company_id"])
    op.create_index("ix_job_role_signals_company_slug", "job_role_signals", ["company_slug"])
    op.create_index("ix_job_role_signals_signal_type", "job_role_signals", ["signal_type"])
    op.create_index("ix_job_role_signals_signal_value", "job_role_signals", ["signal_value"])
    op.execute(
        """
        CREATE VIEW company_primary_career_pages AS
        SELECT company_id, company_slug, company_name, website, yc_is_hiring, yc_job_count,
               career_page_url, page_type, discovery_source, confidence, http_status, evidence,
               checked_at
        FROM company_career_pages WHERE is_primary = true
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS company_primary_career_pages")
    for table in (
        "job_role_signals", "document_embeddings", "document_chunks", "job_extraction_runs",
        "external_job_postings", "page_classifications", "source_documents",
        "career_page_discovery_statuses", "discovered_urls", "company_career_pages",
        "career_page_discovery_events", "yc_job_postings", "companies",
    ):
        op.drop_table(table)
