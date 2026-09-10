"""準備後の短 transaction が lease・取消・lock 待機後の時刻を守ることを検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.db.models import Run, RunAttempt, RunSegment
from projectmind.runs.domain import (
    ClaimedRun,
    LeaseValidationError,
    RunAttemptStatus,
    RunCancellationRequestedError,
    RunSegmentStatus,
    RunStatus,
    lease_token_hash,
)
from projectmind.runs.repository import RunRepository
from tests.worker.test_agent_run_executor import _claimed


def execution_rows() -> tuple[ClaimedRun, Run, RunSegment, RunAttempt]:
    """実 DB に接続せず、同一 aggregate の claim と現行 lease を作る。"""

    claimed = replace(_claimed(), run_segment_id=uuid4())
    run = Run(id=claimed.run_id, project_id=claimed.project_id, status=RunStatus.RUNNING.value)
    segment = RunSegment(
        id=claimed.run_segment_id,
        run_id=run.id,
        segment_no=1,
        status=RunSegmentStatus.RUNNING.value,
    )
    attempt = RunAttempt(
        id=claimed.run_attempt_id,
        run_id=run.id,
        run_segment_id=segment.id,
        status=RunAttemptStatus.RUNNING.value,
        lease_token_hash=lease_token_hash(claimed.lease_token),
        lease_expires_at=claimed.lease_expires_at,
    )
    return claimed, run, segment, attempt


def row_result(value: object) -> MagicMock:
    """scalar query から一行だけ返す最小の結果を作る。"""

    result = MagicMock()
    result.one_or_none.return_value = value
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_start_gate_locks_run_segment_attempt_before_cancellation(cancelled: bool) -> None:
    """開始権の検証は既存の lock 順を使い、古い claim の期限では判断しない。"""

    claimed, run, segment, attempt = execution_rows()
    claimed = replace(claimed, lease_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(
        side_effect=[row_result(run), row_result(segment), row_result(attempt)]
    )
    session.scalar = AsyncMock(return_value=uuid4() if cancelled else None)

    allowed = await RunRepository(session).verify_execution_start(claimed)

    assert allowed is not cancelled
    tables = [
        str(call.args[0].get_final_froms()[0].name) for call in session.scalars.await_args_list
    ]
    assert tables == ["runs", "run_segments", "run_attempts"]
    assert all("FOR UPDATE" in str(call.args[0]) for call in session.scalars.await_args_list)
    assert session.mock_calls[-1][0] == "scalar"
    session.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["token", "expiry", "attempt", "segment", "run", "project"])
async def test_start_gate_rejects_stale_or_foreign_execution(invalid: str) -> None:
    """取消の有無を見る前に、別実行・失効・待機への逆戻りを拒否する。"""

    claimed, run, segment, attempt = execution_rows()
    if invalid == "token":
        attempt.lease_token_hash = "0" * 64
    elif invalid == "expiry":
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif invalid == "attempt":
        attempt.status = RunAttemptStatus.DEFERRED.value
    elif invalid == "segment":
        segment.status = RunSegmentStatus.WAITING.value
    elif invalid == "run":
        run.status = RunStatus.WAITING_FOR_INPUT.value
    else:
        run.project_id = uuid4()
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(
        side_effect=[row_result(run), row_result(segment), row_result(attempt)]
    )
    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(LeaseValidationError):
        await RunRepository(session).verify_execution_start(claimed)
    session.scalar.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_brief_freeze_rejects_durable_cancellation_under_lock() -> None:
    """Executor の poll と保存が競争しても、取消後の新しい Brief を追加しない。"""

    claimed, run, segment, attempt = execution_rows()
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(
        side_effect=[row_result(run), row_result(segment), row_result(attempt)]
    )
    session.scalar = AsyncMock(return_value=uuid4())
    with pytest.raises(RunCancellationRequestedError):
        await RunRepository(session).freeze_agent_task_brief(claimed, brief={}, checksum="unused")
    session.add.assert_not_called()
    session.flush.assert_not_awaited()
    assert session.scalars.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("expired_during_lock", [False, True])
async def test_heartbeat_uses_fresh_locked_row_and_time(
    monkeypatch: pytest.MonkeyPatch,
    expired_during_lock: bool,
) -> None:
    """lock 前の token/時刻を再利用せず、延長時間も取得後から確保する。"""

    claimed, run, segment, attempt = execution_rows()
    locked_at = datetime.now(UTC)
    requested_at = locked_at - timedelta(minutes=2)
    attempt.lease_expires_at = locked_at + timedelta(seconds=-1 if expired_during_lock else 10)
    candidate = RunAttempt(
        id=attempt.id,
        run_id=run.id,
        run_segment_id=segment.id,
        lease_token_hash="old cached token",
    )
    clock = MagicMock(wraps=datetime)
    clock.now.return_value = locked_at
    monkeypatch.setattr("projectmind.runs.repository.datetime", clock)
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=candidate)
    session.scalars = AsyncMock(
        side_effect=[row_result(run), row_result(segment), row_result(attempt)]
    )
    repository = RunRepository(session)
    token_hash = lease_token_hash(claimed.lease_token)
    requested_expiry = requested_at + timedelta(seconds=60)
    if expired_during_lock:
        with pytest.raises(LeaseValidationError, match="expired"):
            await repository.heartbeat_attempt(
                attempt.id, lease_token_hash=token_hash, lease_expires_at=requested_expiry,
                now=requested_at,
            )
    else:
        await repository.heartbeat_attempt(
            attempt.id, lease_token_hash=token_hash, lease_expires_at=requested_expiry,
            now=requested_at,
        )
        assert attempt.heartbeat_at == locked_at
        assert attempt.lease_expires_at == locked_at + timedelta(seconds=60)
    locked_query = session.scalars.await_args_list[-1].args[0]
    assert locked_query.get_execution_options()["populate_existing"] is True
