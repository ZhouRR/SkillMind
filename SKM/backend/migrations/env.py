"""同期・非同期双方の Alembic migration 実行環境を構成する。"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from skillmind.core.settings import get_settings
from skillmind.db import models as models
from skillmind.db.alembic import escape_config_value
from skillmind.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# URL encode 済み password の percent 記号を ConfigParser の interpolation から保護する。
config.set_main_option("sqlalchemy.url", escape_config_value(get_settings().database_url))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """DB 接続を作らず SQL script として migration を生成する。"""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    """既存 connection 上で同期 migration 処理を実行する。"""

    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Application と同じ非同期 driver を使って online migration を実行する。"""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


# Alembic の起動 mode に応じ、同一 metadata を offline/online の両方で利用する。
if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
