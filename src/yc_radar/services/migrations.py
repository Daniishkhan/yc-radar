from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import Column, Index, Table

from yc_radar.services.database import metadata

BASELINE_TABLES = frozenset(
    {
        "companies",
        "yc_job_postings",
        "career_page_discovery_events",
        "company_career_pages",
        "discovered_urls",
        "career_page_discovery_statuses",
        "source_documents",
        "page_classifications",
        "external_job_postings",
        "job_extraction_runs",
        "document_chunks",
        "document_embeddings",
        "job_role_signals",
    }
)
_BASELINE_VIEW = "company_primary_career_pages"
_BASELINE_VIEW_SQL = """
    SELECT company_id, company_slug, company_name, website, yc_is_hiring, yc_job_count,
           career_page_url, page_type, discovery_source, confidence, http_status, evidence,
           checked_at
    FROM company_career_pages WHERE is_primary = true
"""


def alembic_config() -> Config:
    root = Path(__file__).resolve().parents[3]
    return Config(str(root / "alembic.ini"))


def _current_schema(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(text("SELECT current_schema()")))


def _normalize_sql(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).lower()
    normalized = re.sub(r"::(?:[a-z_]+(?:\s+[a-z_]+)?)(?:\[\])?", "", normalized)
    return re.sub(r"[\s();]", "", normalized)


def _normalize_type(value: str) -> str:
    # PostgreSQL reflects an unqualified SQLAlchemy Float as DOUBLE PRECISION.
    return "double precision" if value == "float" else value


def _type_signature(column: Column[Any], engine: Engine) -> str:
    return _normalize_type(column.type.compile(dialect=engine.dialect).lower())


def _actual_type_signature(column: dict[str, Any], engine: Engine) -> str:
    return _normalize_type(str(column["type"].compile(dialect=engine.dialect)).lower())


def _expected_default(column: Column[Any]) -> str | None:
    if column.server_default is None or column.computed is not None:
        return None
    return _normalize_sql(column.server_default.arg)


def _actual_default_matches(column: Column[Any], actual: dict[str, Any]) -> bool:
    expected = _expected_default(column)
    actual_default = _normalize_sql(actual.get("default"))
    if expected is not None:
        return expected == actual_default
    if actual_default is None:
        return True
    # PostgreSQL supplies this implicit sequence default for integer primary keys.
    return bool(column.primary_key and actual_default.startswith("nextval"))


def _expected_foreign_keys(table: Table) -> set[tuple[object, ...]]:
    result: set[tuple[object, ...]] = set()
    for constraint in table.foreign_key_constraints:
        result.add(
            (
                tuple(element.parent.name for element in constraint.elements),
                tuple(element.column.table.name for element in constraint.elements),
                tuple(element.column.name for element in constraint.elements),
                tuple(
                    sorted(
                        (key, str(value).upper())
                        for key, value in {
                            "ondelete": constraint.ondelete,
                            "onupdate": constraint.onupdate,
                        }.items()
                        if value is not None
                    )
                ),
            )
        )
    return result


def _actual_foreign_keys(foreign_keys: list[dict[str, Any]]) -> set[tuple[object, ...]]:
    result: set[tuple[object, ...]] = set()
    for foreign_key in foreign_keys:
        options = foreign_key.get("options") or {}
        result.add(
            (
                tuple(foreign_key.get("constrained_columns") or []),
                (str(foreign_key.get("referred_table")),),
                tuple(foreign_key.get("referred_columns") or []),
                tuple(
                    sorted(
                        (key, str(value).upper())
                        for key, value in options.items()
                        if key in {"ondelete", "onupdate"} and value is not None
                    )
                ),
            )
        )
    return result


def _expected_unique_constraints(table: Table) -> set[tuple[str | None, tuple[str, ...]]]:
    return {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _expected_check_constraints(table: Table) -> set[tuple[str | None, str | None]]:
    return {
        (constraint.name, _normalize_sql(constraint.sqltext))
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _index_options(index: Index) -> dict[str, object | None]:
    options = index.dialect_options["postgresql"]
    return {
        "using": options.get("using") or None,
        "ops": options.get("ops") or {},
        "where": _normalize_sql(options.get("where")),
        "with": options.get("with") or {},
        "include": tuple(options.get("include") or ()),
    }


def _actual_index_options(index: dict[str, Any]) -> dict[str, object | None]:
    options = index.get("dialect_options") or {}
    return {
        "using": options.get("postgresql_using") or None,
        "ops": options.get("postgresql_ops") or {},
        "where": _normalize_sql(options.get("postgresql_where")),
        "with": options.get("postgresql_with") or {},
        "include": tuple(options.get("postgresql_include") or index.get("include_columns") or ()),
    }


def _verify_table_contract(engine: Engine, table: Table, schema: str) -> list[str]:
    inspector = inspect(engine)
    diagnostics: list[str] = []
    actual_columns = {
        column["name"]: column for column in inspector.get_columns(table.name, schema=schema)
    }
    expected_column_names = {column.name for column in table.columns}
    for column in table.columns:
        actual = actual_columns.get(column.name)
        if actual is None:
            diagnostics.append(f"missing column: {table.name}.{column.name}")
            continue
        expected_type = _type_signature(column, engine)
        actual_type = _actual_type_signature(actual, engine)
        if expected_type != actual_type:
            diagnostics.append(
                f"column type mismatch: {table.name}.{column.name} "
                f"expected {expected_type}, found {actual_type}"
            )
        if bool(column.nullable) != bool(actual["nullable"]):
            diagnostics.append(
                f"column nullability mismatch: {table.name}.{column.name} "
                f"expected {column.nullable}, found {actual['nullable']}"
            )
        if not _actual_default_matches(column, actual):
            diagnostics.append(f"column default mismatch: {table.name}.{column.name}")
        expected_computed = column.computed
        actual_computed = actual.get("computed")
        if bool(expected_computed) != bool(actual_computed):
            diagnostics.append(f"computed column mismatch: {table.name}.{column.name}")
        elif expected_computed is not None and actual_computed is not None:
            if _normalize_sql(expected_computed.sqltext) != _normalize_sql(actual_computed.get("sqltext")):
                diagnostics.append(f"computed expression mismatch: {table.name}.{column.name}")
            if bool(expected_computed.persisted) != bool(actual_computed.get("persisted")):
                diagnostics.append(f"computed persistence mismatch: {table.name}.{column.name}")
    for column_name in sorted(set(actual_columns) - expected_column_names):
        diagnostics.append(f"unexpected column: {table.name}.{column_name}")

    expected_primary_key = tuple(column.name for column in table.primary_key.columns)
    actual_primary_key = tuple(inspector.get_pk_constraint(table.name, schema=schema).get("constrained_columns") or [])
    if expected_primary_key != actual_primary_key:
        diagnostics.append(
            f"primary key mismatch: {table.name} expected {expected_primary_key}, found {actual_primary_key}"
        )

    expected_foreign_keys = _expected_foreign_keys(table)
    actual_foreign_keys = _actual_foreign_keys(inspector.get_foreign_keys(table.name, schema=schema))
    if expected_foreign_keys != actual_foreign_keys:
        diagnostics.append(f"foreign key mismatch: {table.name}")

    expected_unique = _expected_unique_constraints(table)
    actual_unique = {
        (item.get("name"), tuple(item.get("column_names") or []))
        for item in inspector.get_unique_constraints(table.name, schema=schema)
    }
    expected_unique_columns = {columns for _, columns in expected_unique}
    actual_unique_columns = {columns for _, columns in actual_unique}
    if expected_unique_columns != actual_unique_columns:
        diagnostics.append(f"unique constraint mismatch: {table.name}")
    for name, columns in expected_unique:
        if name is not None and (name, columns) not in actual_unique:
            diagnostics.append(f"unique constraint name mismatch: {table.name}.{name}")

    expected_checks = _expected_check_constraints(table)
    actual_checks = {
        (item.get("name"), _normalize_sql(item.get("sqltext")))
        for item in inspector.get_check_constraints(table.name, schema=schema)
    }
    if expected_checks != actual_checks:
        diagnostics.append(f"check constraint mismatch: {table.name}")

    actual_indexes = {
        item["name"]: item
        for item in inspector.get_indexes(table.name, schema=schema)
        if not item.get("duplicates_constraint")
    }
    expected_indexes = {index.name: index for index in table.indexes if index.name}
    if set(expected_indexes) != set(actual_indexes):
        diagnostics.append(f"index name mismatch: {table.name}")
    for name, index in expected_indexes.items():
        actual = actual_indexes.get(name)
        if actual is None:
            continue
        expected_columns = tuple(column.name for column in index.columns)
        actual_columns_for_index = tuple(actual.get("column_names") or [])
        if expected_columns != actual_columns_for_index:
            diagnostics.append(f"index columns mismatch: {table.name}.{name}")
        if bool(index.unique) != bool(actual.get("unique")):
            diagnostics.append(f"index uniqueness mismatch: {table.name}.{name}")
        if _index_options(index) != _actual_index_options(actual):
            diagnostics.append(f"index options mismatch: {table.name}.{name}")
    return diagnostics


def verify_existing_baseline(engine: Engine) -> list[str]:
    """Return read-only diagnostics before stamping an unversioned legacy schema.

    This intentionally fails closed: names alone are insufficient because stamping skips the
    historical baseline DDL permanently. The contract includes columns, constraints, indexes,
    the pgvector extension, and the legacy view definition.
    """
    inspector = inspect(engine)
    schema = _current_schema(engine)
    actual_tables = set(inspector.get_table_names(schema=schema))
    missing = sorted(BASELINE_TABLES - actual_tables)
    diagnostics = [f"missing table: {name}" for name in missing]
    for table_name in sorted(BASELINE_TABLES & actual_tables):
        diagnostics.extend(_verify_table_contract(engine, metadata.tables[table_name], schema))
    with engine.connect() as connection:
        vector_enabled = bool(
            connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            )
        )
    if not vector_enabled:
        diagnostics.append("missing extension: vector")
    views = set(inspector.get_view_names(schema=schema))
    if _BASELINE_VIEW not in views:
        diagnostics.append(f"missing view: {_BASELINE_VIEW}")
    elif _normalize_sql(inspector.get_view_definition(_BASELINE_VIEW, schema=schema)) != _normalize_sql(
        _BASELINE_VIEW_SQL
    ):
        diagnostics.append(f"view definition mismatch: {_BASELINE_VIEW}")
    return diagnostics


def upgrade_database(engine: Engine, revision: str = "head") -> None:
    """Apply migrations to an empty/versioned database without guessing legacy state."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names(schema=_current_schema(engine)))
    if "alembic_version" not in table_names and BASELINE_TABLES & table_names:
        diagnostics = verify_existing_baseline(engine)
        detail = "; ".join(diagnostics) if diagnostics else "baseline tables are present"
        raise RuntimeError(
            "Existing unversioned schema detected; do not auto-migrate it. "
            "Run `uv run python scripts/migrate_database.py verify-existing`, then "
            "`uv run alembic stamp 0001_baseline` and `uv run alembic upgrade head`. "
            f"Verification: {detail}."
        )
    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    connection = config.attributes["connection"]
    try:
        command.upgrade(config, revision)
    finally:
        connection.close()


def _drop_unversioned_legacy_schema(engine: Engine, schema: str) -> None:
    """Remove only known legacy objects after the caller has confirmed a destructive rebuild."""
    quoted_schema = engine.dialect.identifier_preparer.quote_schema(schema)
    with engine.begin() as connection:
        connection.execute(text(f"DROP VIEW IF EXISTS {quoted_schema}.{_BASELINE_VIEW}"))
        for table_name in sorted(BASELINE_TABLES):
            connection.execute(
                text(f"DROP TABLE IF EXISTS {quoted_schema}.{table_name} CASCADE")
            )


def rebuild_database(engine: Engine) -> None:
    """Destructively rebuild migration history, including an unversioned legacy schema."""
    schema = _current_schema(engine)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names(schema=schema))
    if "alembic_version" not in table_names and BASELINE_TABLES & table_names:
        _drop_unversioned_legacy_schema(engine, schema)
    config = alembic_config()
    config.attributes["connection"] = engine.connect()
    connection = config.attributes["connection"]
    try:
        if "alembic_version" in table_names:
            command.downgrade(config, "base")
        command.upgrade(config, "head")
    finally:
        connection.close()
