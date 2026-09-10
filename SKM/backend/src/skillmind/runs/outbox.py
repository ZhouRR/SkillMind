"""Transactional Outbox を外部 Queue/通知へ relay する use case。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.runs.domain import PendingOutboxMessage, RelayResult
from skillmind.runs.repository import OutboxRepository

OutboxPublisher = Callable[[PendingOutboxMessage], Awaitable[None]]


class OutboxRelay:
    """Outbox row lock と外部 publish の整合性境界を提供する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        batch_size: int,
    ) -> None:
        """Session factory と一回の最大処理件数を保持する。"""

        self._session_factory = session_factory
        self._batch_size = batch_size

    async def relay_once(
        self,
        *,
        topics: frozenset[str],
        publisher: OutboxPublisher,
    ) -> RelayResult:
        """未公開 message を一 batch publish し、結果を同じ transaction に記録する。"""

        published = 0
        failed = 0
        async with self._session_factory() as session, session.begin():
            repository = OutboxRepository(session)
            messages = await repository.lock_pending(topics=topics, limit=self._batch_size)
            for message in messages:
                try:
                    # Row lock 中に publish し、複数 Worker による同時配送を防ぐ。
                    await publisher(message)
                    repository.mark_published(message.message_id, published_at=datetime.now(UTC))
                    published += 1
                except (
                    Exception
                ) as exc:  # 外部 Queue 境界の失敗を次回 relay 可能な状態へ正規化する。
                    repository.mark_failed(message.message_id, error=exc)
                    failed += 1
        return RelayResult(selected=published + failed, published=published, failed=failed)
