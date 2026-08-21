"""Alembic environment, wired for the async engine.

Migrations run through the same asyncpg driver the services use, so there is no
second database driver to install or keep in sync. The URL is built from
environment variables rather than hardcoded in alembic.ini, which lets the same
migration set target local Docker, a Testcontainers instance, and CI unchanged.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import every model module so that Base.metadata is fully populated before
# autogenerate compares it against the live database. Missing an import here
# silently produces a migration that drops tables.
from packages.db.base import Base
from packages.db import models  # noqa: F401  (registers all 13 tables)
from packages.db.session import build_database_url

config = context.config
config.set_main_option("sqlalchemy.url", build_database_url(async_driver=True))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type changes, not just added/dropped columns.
        compare_type=True,
        # Detect changes to server-side defaults.
        compare_server_default=True,
        # Wrap each migration in its own transaction so a partial failure
        # cannot leave the schema half-applied.
        transaction_per_migration=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting -- used to review a migration."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
