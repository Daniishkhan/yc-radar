from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from yc_radar.core.config import Settings
from yc_radar.services.database import create_schema, engine_from_url


@pytest.fixture()
def postgres_database_url() -> str:
    database_url = (
        os.environ.get("YC_RADAR_TEST_DATABASE_URL") or Settings(_env_file=None).database_url
    )
    parsed_url = make_url(database_url)
    if not parsed_url.drivername.startswith("postgresql"):
        pytest.skip("Postgres integration tests require a postgresql database URL.")

    admin_engine = create_engine(database_url, future=True, pool_pre_ping=True)
    database_name = f"yc_radar_test_{uuid.uuid4().hex}"
    quoted_database = admin_engine.dialect.identifier_preparer.quote_identifier(database_name)
    created = False

    try:
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f"CREATE DATABASE {quoted_database}")
        created = True
    except SQLAlchemyError as exc:
        admin_engine.dispose()
        pytest.skip(f"Postgres test database cannot be created safely: {exc}")

    test_query = {key: value for key, value in parsed_url.query.items() if key != "options"}
    test_url = parsed_url.set(database=database_name, query=test_query).render_as_string(
        hide_password=False
    )
    engine = engine_from_url(test_url)
    try:
        create_schema(engine, checkfirst=False)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == "public"
            assert connection.scalar(
                text("SELECT to_regclass(:qualified_name)"),
                {"qualified_name": "public.companies"},
            ) == "companies"
            assert connection.scalar(
                text("SELECT to_regclass(:qualified_name)"),
                {"qualified_name": "ingest.runs"},
            ) == "ingest.runs"
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0002_ingest_staging"
            )
        yield test_url
    finally:
        engine.dispose()
        if created:
            with admin_engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {quoted_database} WITH (FORCE)")
        admin_engine.dispose()
