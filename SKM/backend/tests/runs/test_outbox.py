"""Transactional Outbox relay の排他取得と配送結果を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import OutboxMessage
from skillmind.runs.outbox import OutboxRelay


def create_session_factory(
    messages: list[OutboxMessage],
) -> tuple[MagicMock, AsyncSession]:
    """Relay transaction と lock 対象を提供する session factory mock を生成する。"""

    session = MagicMock(spec=AsyncSession)
    scalar_result = MagicMock()
    scalar_result.all.return_value = messages
    session.scalars = AsyncMock(return_value=scalar_result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    session.begin.return_value = transaction
    factory = MagicMock(return_value=session)
    return factory, session


def create_message(*, topic: str) -> OutboxMessage:
    """未公開 OutboxMessage model を生成する。"""

    return OutboxMessage(
        id=uuid4(),
        aggregate_type="run",
        aggregate_id=uuid4(),
        topic=topic,
        payload_json={"status": "QUEUED"},
        occurred_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        published_at=None,
        publish_attempts=0,
        error_json=None,
    )


@pytest.mark.asyncio
async def test_relay_marks_successful_message_as_published() -> None:
    """Publish 成功時に timestamp と attempt count が更新されることを確認する。"""

    message = create_message(topic="run.lifecycle.changed/v1")
    factory, _ = create_session_factory([message])
    publisher = AsyncMock()

    result = await OutboxRelay(factory, batch_size=20).relay_once(
        topics=frozenset({message.topic}), publisher=publisher
    )

    assert result.selected == 1
    assert result.published == 1
    assert result.failed == 0
    assert message.published_at is not None
    assert message.publish_attempts == 1
    assert message.error_json is None


@pytest.mark.asyncio
async def test_relay_keeps_failed_message_pending() -> None:
    """Publish 失敗時に message を未公開のまま残して診断情報を記録する。"""

    message = create_message(topic="run.dispatch.requested/v1")
    factory, _ = create_session_factory([message])
    publisher = AsyncMock(side_effect=RuntimeError("queue unavailable"))

    result = await OutboxRelay(factory, batch_size=20).relay_once(
        topics=frozenset({message.topic}), publisher=publisher
    )

    assert result.failed == 1
    assert message.published_at is None
    assert message.publish_attempts == 1
    assert message.error_json == {
        "type": "RuntimeError",
        "message": "outbox_publish_failed",
    }
