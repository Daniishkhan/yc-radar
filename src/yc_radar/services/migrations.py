from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from yc_radar.core.config import get_settings


_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def alembic_config(database_url: str | None = None) -> Config:
    """Build the project Alembic configuration for the configured local database."""
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    url = database_url or get_settings().database_url
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade_database(engine: Engine) -> None:
    """Upgrade through Alembic, the sole schema authority."""
    with engine.begin() as connection:
        config = alembic_config(str(engine.url))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def rebuild_database(engine: Engine) -> None:
    """Drop the configured core schema and fixed ingest schema, then migrate to head.

    This is intentionally destructive. YC Radar is a rebuildable local workbench and
    the clean-break baseline has no compatibility path from the experimental schema.
    """
    with engine.begin() as connection:
        schema = str(connection.dialect.default_schema_name)
        if schema == "ingest":
            raise ValueError("The core database schema cannot be named 'ingest'")
        connection.exec_driver_sql('DROP SCHEMA IF EXISTS "ingest" CASCADE')
        inspector = inspect(connection)
        preparer = connection.dialect.identifier_preparer
        quoted_schema = preparer.quote_schema(schema)

        for view_name in inspector.get_materialized_view_names(schema=schema):
            quoted_view = preparer.quote_identifier(view_name)
            connection.exec_driver_sql(
                f"DROP MATERIALIZED VIEW IF EXISTS {quoted_schema}.{quoted_view} CASCADE"
            )
        for view_name in inspector.get_view_names(schema=schema):
            quoted_view = preparer.quote_identifier(view_name)
            connection.exec_driver_sql(f"DROP VIEW IF EXISTS {quoted_schema}.{quoted_view} CASCADE")
        for table_name in inspector.get_table_names(schema=schema):
            quoted_table = preparer.quote_identifier(table_name)
            connection.exec_driver_sql(
                f"DROP TABLE IF EXISTS {quoted_schema}.{quoted_table} CASCADE"
            )

        config = alembic_config(str(engine.url))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
