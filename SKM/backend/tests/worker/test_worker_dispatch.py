"""ARQ Outbox relay と RunExecutor handoff の feature gate を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from skillmind.core.settings import Settings
from skillmind.runs.domain import ClaimedRun, PendingOutboxMessage, RelayResult
from skillmind.runs.outbox import OutboxPublisher
from skillmind.worker.settings import execute_run, relay_outbox


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
        "settings": Settings(
            worker_dispatch_enabled=True, deferred_features_enabled=True, _env_file=None,
        ),
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
        input_json={"ticket_id": "ISSUE-1234"},
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
        "settings": Settings(run_lease_seconds=60, worker_dispatch_enabled=True, _env_file=None),
        "worker_id": "worker-1",
    }

    result = await execute_run(ctx, str(claimed.run_id))

    assert result["status"] == "accepted"
    executor.execute.assert_awaited_once_with(claimed)


@pytest.mark.parametrize("effects_enabled", [False, True])
async def test_saved_effect_handoff_happens_after_agent_cleanup_and_before_relay(effects_enabled):
    """自動承認済み候補を直接渡しても、モデル清理・feature gate・永続配送の順序を守る。"""

    from tests.worker.test_agent_run_executor import _claimed

    claimed = _claimed()
    order = []

    async def run_done(value):
        """Engine 清理も含む Run executor の完了を表す。"""

        assert value is claimed
        order.append("agent_closed")

    async def effect_done(value):
        """原 Attempt のみを独立 Effect executor へ渡す。"""

        assert value is claimed and order == ["agent_closed"]
        order.append("effect_applied")

    async def relay_done(**kwargs):
        """新 Segment の dispatch は外部 write 完了後に配送される。"""

        expected = ["agent_closed", "effect_applied"] if effects_enabled else ["agent_closed"]
        assert order == expected
        order.append("relay")
        return RelayResult(selected=0, published=0, failed=0)

    executor = MagicMock(execute=AsyncMock(side_effect=run_done))
    effects = MagicMock(execute_pending_for_attempt=AsyncMock(side_effect=effect_done))
    ctx = {
        "run_executor": executor, "effect_executor": effects,
        "run_service": MagicMock(claim_run=AsyncMock(return_value=claimed)),
        "settings": Settings(
            worker_dispatch_enabled=True, database_writes_enabled=effects_enabled, _env_file=None,
        ),
        "worker_id": "worker-test", "redis": MagicMock(),
        "outbox_relay": MagicMock(relay_once=AsyncMock(side_effect=relay_done)),
    }
    assert (await execute_run(ctx, str(claimed.run_id)))["status"] == "accepted"
    assert order[-1] == "relay"
    assert effects.execute_pending_for_attempt.await_count == int(effects_enabled)


async def test_failed_immediate_relay_preserves_completed_run(monkeypatch):
    """Redis 障害を Run 再実行に変えず、元 Outbox の通常回収へ委ねる。"""

    from skillmind.worker import settings as worker

    monkeypatch.setattr(worker, "relay_outbox", AsyncMock(side_effect=ConnectionError))
    await worker._relay_after_execution({"outbox_relay": object()})


async def test_capacity_retry_dispatch_keeps_absolute_deadline() -> None:
    """Outbox の再配送でも退避期間をリセットせず、同じ期限を ARQ へ渡す。"""
    deadline = datetime(2099, 1, 1, tzinfo=UTC)
    message = PendingOutboxMessage(
        message_id=uuid4(),
        aggregate_id=uuid4(),
        topic="run.dispatch.requested/v1",
        payload={"retry_at": deadline.isoformat()},
        aggregate_type="run",
        publish_attempts=0,
    )
    relay = MagicMock()

    async def publish_retry(*, topics, publisher):
        """持久済みの同じ message を relay する。"""
        await publisher(message)
        return RelayResult(selected=1, published=1, failed=0)

    relay.relay_once = AsyncMock(side_effect=publish_retry)
    redis = MagicMock()
    redis.enqueue_job = AsyncMock()
    await relay_outbox(
        {
            "settings": Settings(worker_dispatch_enabled=True),
            "redis": redis,
            "outbox_relay": relay,
            "run_executor": MagicMock(),
        }
    )
    assert redis.enqueue_job.await_args.kwargs["_defer_until"] == deadline
