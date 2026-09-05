"""ARQ Outbox relay と RunExecutor handoff の feature gate を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from projectmind.core.settings import Settings
from projectmind.runs.domain import ClaimedRun, PendingOutboxMessage, RelayResult
from projectmind.runs.outbox import OutboxPublisher
from projectmind.worker.settings import execute_run, relay_outbox


class CapturingRelay:
    """対象 topic を記録し、各 topic の publisher を一度実行する fake relay。"""

    def __init__(self) -> None:
        """取得した topic を空集合で初期化する。"""

        self.topics: frozenset[str] = frozenset()

    async def relay_once(
        self, *, topics: frozenset[str], publisher: OutboxPublisher
    ) -> RelayResult:
        """指定 topic ごとに fake message を publish する。"""

        self.topics = topics
        for topic in topics:
            await publisher(
                PendingOutboxMessage(
                    message_id=uuid4(),
                    aggregate_type="run",
                    aggregate_id=uuid4(),
                    topic=topic,
                    payload={"status": "QUEUED"},
                    publish_attempts=0,
                )
            )
        return RelayResult(selected=len(topics), published=len(topics), failed=0)


@pytest.mark.asyncio
async def test_dispatch_topic_stays_pending_without_executor() -> None:
    """Feature flag が有効でも RunExecutor 未注入なら dispatch を選択しない。"""

    relay = CapturingRelay()
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    redis.enqueue_job = AsyncMock()
    ctx = {
        "settings": Settings(worker_dispatch_enabled=True, _env_file=None),
        "redis": redis,
        "outbox_relay": relay,
    }

    result = await relay_outbox(ctx)

    assert relay.topics == frozenset({"run.lifecycle.changed/v1"})
    assert result["dispatch"] == "disabled"
    redis.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_uses_deterministic_arq_job_id_when_ready() -> None:
    """Executor 注入時だけ dispatch topic が一意 job ID で enqueue される。"""

    relay = CapturingRelay()
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    redis.enqueue_job = AsyncMock(return_value=MagicMock())
    ctx = {
        "settings": Settings(worker_dispatch_enabled=True, _env_file=None),
        "redis": redis,
        "outbox_relay": relay,
        "run_executor": MagicMock(),
    }

    result = await relay_outbox(ctx)

    assert "run.dispatch.requested/v1" in relay.topics
    assert result["dispatch"] == "enabled"
    kwargs = redis.enqueue_job.await_args.kwargs
    assert kwargs["_job_id"].startswith("run-dispatch:")


@pytest.mark.asyncio
async def test_effect_dispatch_requires_executor_and_uses_own_job_identity() -> None:
    """Effect executor が存在する Worker だけが effect job を一意 ID で enqueue する。"""

    relay = CapturingRelay()
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    redis.enqueue_job = AsyncMock(return_value=MagicMock())
    ctx = {
        "settings": Settings(worker_dispatch_enabled=True, _env_file=None),
        "redis": redis,
        "outbox_relay": relay,
        "effect_executor": MagicMock(),
    }

    result = await relay_outbox(ctx)

    assert "effect.apply.requested/v1" in relay.topics
    assert "run.dispatch.requested/v1" not in relay.topics
    assert result["dispatch"] == "enabled"
    call = redis.enqueue_job.await_args
    assert call.args[0] == "execute_effect"
    assert call.kwargs["_job_id"].startswith("effect-dispatch:")


@pytest.mark.asyncio
async def test_execute_run_checks_executor_before_claim() -> None:
    """Executor 未設定時に Run を PREPARING へ進めないことを確認する。"""

    service = MagicMock()
    service.claim_run = AsyncMock()
    with pytest.raises(RuntimeError, match="RunExecutor is not configured"):
        await execute_run({"run_service": service}, str(uuid4()))
    service.claim_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_run_claims_and_hands_off_snapshot() -> None:
    """Queue job が lease 取得済み snapshot を executor へ一度だけ渡す。"""

    claimed = ClaimedRun(
        run_id=uuid4(),
        run_attempt_id=uuid4(),
        project_id=uuid4(),
        actor_id=uuid4(),
        attempt_no=1,
        lease_token="lease-token",
        lease_expires_at=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
        row_version=2,
        input_json={"ticket_id": "JAF-1234"},
        task_snapshot_json={},
        permission_snapshot_json={},
        selected_sources_json={},
        limits_snapshot_json={},
    )
    service = MagicMock()
    service.claim_run = AsyncMock(return_value=claimed)
    executor = MagicMock()
    executor.execute = AsyncMock()
    ctx = {
        "run_executor": executor,
        "run_service": service,
        "settings": Settings(run_lease_seconds=60, _env_file=None),
        "worker_id": "worker-1",
    }

    result = await execute_run(ctx, str(claimed.run_id))

    assert result["status"] == "accepted"
    executor.execute.assert_awaited_once_with(claimed)
