"""Database と Redis の process resource factory を提供する。"""

from __future__ import annotations

from typing import cast

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from skillmind.core.settings import Settings


def create_database_engine(settings: Settings) -> AsyncEngine:
    """切断検知を有効にした非同期 Database engine を生成する。"""

    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Transaction ごとに利用する AsyncSession factory を生成する。"""

    return async_sessionmaker(engine, expire_on_commit=False)


def create_redis_client(settings: Settings) -> Redis:
    """Queue と短期通知に利用する非同期 Redis client を生成する。"""

    return cast(Redis, Redis.from_url(settings.redis_url, decode_responses=True))
