"""Alembic env for paykit's own tables.

Deliberately reads `PAYKIT_DATABASE_URL` / `PAYKIT_TABLE_PREFIX` straight
from the environment rather than through `PayKitSettings` - applying
migrations (e.g. in a CI job or ops runbook) shouldn't require Razorpay
keys to be configured, and PayKitSettings makes those required.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from paykit.storage.sqlalchemy.tables import PayKitTables

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_prefix = os.environ.get("PAYKIT_TABLE_PREFIX", "paykit_")
target_metadata = PayKitTables(_prefix).metadata


def _database_url() -> str:
    url = os.environ.get("PAYKIT_DATABASE_URL")
    if not url:
        raise RuntimeError("PAYKIT_DATABASE_URL must be set to run paykit migrations")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable: AsyncEngine = create_async_engine(_database_url())
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
