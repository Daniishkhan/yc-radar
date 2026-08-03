from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from yc_radar.core.config import get_settings
from yc_radar.services.database import metadata

config = context.config
# CLI migrations follow DATABASE_URL/.env rather than the sample URL in alembic.ini.
if config.attributes.get("connection") is None:
    config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata
_OWNED_TABLES = {
    None: {table.name for table in metadata.tables.values() if table.schema is None},
    "ingest": {table.name for table in metadata.tables.values() if table.schema == "ingest"},
}


def _include_name(name: str | None, type_: str, _parent_names) -> bool:
    if type_ == "schema":
        # ``None`` is Alembic's name for the connection's default schema. The
        # fixed ingest schema is the only additional namespace owned by YC Radar.
        return name in {None, "ingest"}
    if type_ == "table":
        schema_name = _parent_names.get("schema_name")
        return name != "alembic_version" and name in _OWNED_TABLES.get(schema_name, set())
    return True


def _include_object(object_, name: str | None, type_: str, _reflected, _compare_to) -> bool:
    if type_ != "table":
        return True
    schema_name = getattr(object_, "schema", None)
    return name != "alembic_version" and name in _OWNED_TABLES.get(schema_name, set())


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_schemas=True,
        include_name=_include_name,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _configure_online(connection) -> None:
    # Reading through the connection would autobegin a transaction before Alembic can own it;
    # the dialect resolves this from the connection's configured search_path at connect time.
    schema = str(connection.dialect.default_schema_name)
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_schemas=True,
        version_table_schema=schema,
        include_name=_include_name,
        include_object=_include_object,
    )


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        _configure_online(supplied_connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _configure_online(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
