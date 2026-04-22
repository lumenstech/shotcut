"""Alembic environment, supporting both sync and async SQLAlchemy URLs.

Production uses `postgresql+asyncpg://...` (async). Tests drive the
migration with sync `sqlite:///...` URLs via `alembic.command.upgrade`.
We detect which is which from the URL and route accordingly.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from shotcut.config import settings
from shotcut.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    # Prefer an explicitly-set URL (tests), fall back to settings (prod).
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return settings.database_url


def _is_async_url(url: str) -> bool:
    # aiosqlite / asyncpg / aiomysql / asyncmy all expose async drivers via
    # a `+driver` suffix in the URL. Vanilla drivers (sqlite://, postgresql://)
    # are sync.
    async_markers = ("+aiosqlite", "+asyncpg", "+aiomysql", "+asyncmy")
    return any(marker in url for marker in async_markers)


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _get_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_sync_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


def run_migrations_online() -> None:
    url = _get_url()
    if _is_async_url(url):
        asyncio.run(run_async_migrations())
    else:
        run_sync_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
