"""Alembic environment.

The database URL is never stored in alembic.ini. It comes from DATABASE_URL
(via Settings) unless overridden with `alembic -x db_url=...`.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from crypto_processing_api.config import Settings
from crypto_processing_api.ledger.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return str(override)
    if os.environ.get("DATABASE_URL"):
        return Settings().sqlalchemy_url  # type: ignore[call-arg]
    raise RuntimeError("DATABASE_URL is not set and no -x db_url= was given")


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
