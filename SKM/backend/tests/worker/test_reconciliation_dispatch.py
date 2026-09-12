"""只読核対の Outbox/ARQ 接線が原要求 ID と停止後の読取境界を保つことを検証する。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from skillmind.core.settings import Settings
from skillmind.effects.reconciliation_requests import (
    RECONCILIATION_DISPATCH_TOPIC,
    ReconciliationRequestNotFoundError,
)
from skillmind.worker.settings import (
    WorkerSettings,
    execute_reconciliation_request_job,
    recover_reconciliation_requests,
    relay_outbox,
)
from tests.worker.test_worker_dispatch import CapturingRelay


async def test_read_dispatch_is_independent_of_write_gates_and_id_only():
    """全 write flag が閉じていても、原要求だけを現在の只読 executor へ配送できる。"""
    relay = CapturingRelay()
    redis = MagicMock(publish=AsyncMock(), enqueue_job=AsyncMock())
    result = await relay_outbox(
        {
            "settings": Settings(
                worker_dispatch_enabled=True,
                database_writes_enabled=False,
                deferred_features_enabled=False,
                _env_file=None,
            ),
            "redis": redis,
            "outbox_relay": relay,
            "reconciliation_executor": MagicMock(),
        }
    )
    assert result["dispatch"] == "enabled"
    assert relay.topics == {"run.lifecycle.changed/v1", RECONCILIATION_DISPATCH_TOPIC}
    call = redis.enqueue_job.await_args
    assert call.args[0] == "execute_reconciliation_request_job"
    assert len(call.args) == 2 and UUID(call.args[1]).int != 0
    assert set(call.kwargs) == {"_job_id", "_queue_name"}
    assert call.kwargs["_job_id"].startswith("reconciliation-dispatch:")


@pytest.mark.parametrize("enabled,has_executor", [(False, True), (True, False)])
async def test_unavailable_dispatch_leaves_request_pending(enabled, has_executor):
    """Worker gate/装配が不足すると Outbox を消費せず、要求への資格を補造しない。"""
    relay = CapturingRelay()
    ctx = {
        "settings": Settings(worker_dispatch_enabled=enabled, _env_file=None),
        "redis": MagicMock(publish=AsyncMock(), enqueue_job=AsyncMock()),
        "outbox_relay": relay,
    }
    if has_executor:
        ctx["reconciliation_executor"] = MagicMock()
    await relay_outbox(ctx)
    assert RECONCILIATION_DISPATCH_TOPIC not in relay.topics
    ctx["redis"].enqueue_job.assert_not_awaited()


@pytest.mark.parametrize("identity", ["invalid", str(UUID(int=0)), {"request_id": str(uuid4())}])
async def test_queue_cannot_supply_actor_target_or_invalid_identity(identity):
    """未知の job 本文を executor 呼出し前に拒否する。"""
    executor = MagicMock(execute=AsyncMock())
    result = await execute_reconciliation_request_job(
        {
            "settings": Settings(worker_dispatch_enabled=True, _env_file=None),
            "reconciliation_executor": executor,
        },
        identity,
    )
    assert result["status"] == "rejected"
    executor.execute.assert_not_awaited()


async def test_job_and_recovery_use_original_ledger_and_are_registered():
    """job は UUID 一つ、回収は上限だけを渡し、business write 引数を持たない。"""
    executor = MagicMock(execute=AsyncMock(return_value="observed"))
    ledger = MagicMock(recover_expired=AsyncMock(return_value=1))
    settings = Settings(worker_dispatch_enabled=True, _env_file=None)
    ctx = {
        "settings": settings,
        "reconciliation_executor": executor,
        "reconciliation_requests": ledger,
    }
    identity = uuid4()
    assert await execute_reconciliation_request_job(ctx, str(identity)) == {
        "status": "observed",
        "request_id": str(identity),
    }
    executor.execute.assert_awaited_once_with(identity)
    assert await recover_reconciliation_requests(ctx) == {"changed": 1}
    ledger.recover_expired.assert_awaited_once_with(limit=settings.outbox_batch_size)
    assert execute_reconciliation_request_job in WorkerSettings.functions
    assert any(job.coroutine is recover_reconciliation_requests for job in WorkerSettings.cron_jobs)
    executor.execute.side_effect = ReconciliationRequestNotFoundError()
    assert (await execute_reconciliation_request_job(ctx, str(uuid4())))["status"] == "rejected"
