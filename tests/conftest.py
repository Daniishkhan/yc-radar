from __future__ import annotations

import os
import uuid
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from yc_radar.core.config import Settings
from yc_radar.services.database import create_schema, engine_from_url


@pytest.fixture()
def postgres_database_url() -> str:
    database_url = (
        os.environ.get("YC_RADAR_TEST_DATABASE_URL") or Settings(_env_file=None).database_url
    )
    if not make_url(database_url).drivername.startswith("postgresql"):
        pytest.skip("Postgres integration tests require a postgresql database URL.")

    admin_engine = create_engine(database_url, future=True, pool_pre_ping=True)
    schema = f"test_{uuid.uuid4().hex}"

    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError as exc:
        admin_engine.dispose()
        pytest.skip(f"Postgres is not available for integration tests: {exc}")

    separator = "&" if "?" in database_url else "?"
    scoped_url = f"{database_url}{separator}options={quote(f'-csearch_path={schema},public')}"
    engine = engine_from_url(scoped_url)
    create_schema(engine, checkfirst=False)
    engine.dispose()

    try:
        yield scoped_url
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()
