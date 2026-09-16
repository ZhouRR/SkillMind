"""容量待ちの期限、原 Session 継承、取消競争と永続予約を検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from skillmind.agent.domain import AgentEventType
from skillmind.db.models import AgentSession, OutboxMessage, RunEvent
from skillmind.runs.capacity_retry import (
    MODEL_CAPACITY_CODE,
    capacity_retry_at,
    capacity_retry_delay,
)
from skillmind.runs.domain import (
    AgentSessionMetadata,
    LeaseValidationError,
    RunAttemptStatus,
    RunSegmentStatus,
    RunStatus,
    SessionContinuationMode,
)
from skillmind.runs.repository import RunRepository
from sqlalchemy.ext.asyncio import AsyncSession
from tests.runs.execution_event_fakes import execution_event
from tests.runs.test_run_repository import create_claimed_running, create_queued_run, create_segment


def scalar_result(value):
    """一件の DB 読取結果だけを差し替える。"""
    result = MagicMock()
    result.one_or_none.return_value = value
    return result


@pytest.mark.parametrize(
    "attempt,maximum,delay",
    [(1, 3, 15), (2, 3, 30), (3, 3, None), (1, 1, None), (2, 2, None), (3, 8, None)],
)
def test_retry_is_bounded(attempt, maximum, delay):
    """既存上限を超えず、容量不足を無限再送しない。"""
    assert capacity_retry_delay(attempt, maximum) == delay


@pytest.mark.parametrize(
    "error",
    [
        {"code": MODEL_CAPACITY_CODE, "retryable": True},
        {"code": MODEL_CAPACITY_CODE, "retryable": True, "retry_at": "2026-01-01T00:00:00"},
    ],
)
def test_invalid_deadline_fails_closed(error):
    """期限の破損で即時再送に落とさない。"""
    with pytest.raises(ValueError):
        capacity_retry_at(error)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_retry_finalization_preserves_segment_and_cancellation(cancelled):
    """Attempt だけ閉じて同一 Segment の再開を予約し、取消後には予約しない。"""
    claimed, run, attempt = create_claimed_running()
    segment = create_segment(run, status=RunSegmentStatus.RUNNING)
    segment.checkpoint_json = {"summary": "Saved document", "artifact_refs": ["artifact-existing"]}
    attempt.run_segment_id = segment.id
    claimed = replace(claimed, run_segment_id=segment.id)
    event = replace(
        execution_event(claimed, AgentEventType.ENGINE_FAILED),
        payload={"code": MODEL_CAPACITY_CODE, "retryable": True},
    )
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(
        side_effect=[
            scalar_result(run),
            scalar_result(segment),
            scalar_result(attempt),
            scalar_result(None),
        ]
    )
    session.scalar = AsyncMock(side_effect=[uuid4() if cancelled else None, 10])
    stored = await RunRepository(session).finalize_execution(
        claimed,
        target=RunStatus.RETRY_PENDING,
        attempt_status=RunAttemptStatus.FAILED,
        event=event,
        session_metadata=AgentSessionMetadata(
            cwd="/run/workspace",
            engine="codex-sdk",
            sdk_version="test",
            cli_version="test",
            model="test",
        ),
        result=None,
        error_json={"code": MODEL_CAPACITY_CODE, "retryable": True},
        retry_delay_seconds=15,
    )
    singles = [call.args[0] for call in session.add.call_args_list]
    dispatches = [item for item in singles if isinstance(item, OutboxMessage)]
    assert segment.checkpoint_json == {
        "summary": "Saved document",
        "artifact_refs": ["artifact-existing"],
    }
    assert attempt.lease_token_hash is None
    assert attempt.lease_expires_at is None
    if cancelled:
        assert stored is RunStatus.CANCELLED
        assert not dispatches
        assert attempt.status == "CANCELLED"
        assert run.error_json is None
    else:
        assert stored is RunStatus.RETRY_PENDING
        assert segment.status == "RUNNING" and segment.finished_at is None
        assert run.finished_at is None and attempt.status == "FAILED"
        assert len(dispatches) == 1
        assert dispatches[0].payload_json["retry_at"] == run.error_json["retry_at"]
        assert capacity_retry_at(run.error_json) > datetime.now(UTC)
        assert next(item for item in singles if isinstance(item, AgentSession)).status == "FAILED"
        events = [x for x in session.add_all.call_args.args[0] if isinstance(x, RunEvent)]
        assert events[-1].payload_json["status"] == "RETRY_PENDING"


@pytest.mark.parametrize("too_early,missing_parent", [(True, False), (False, False), (False, True)])
async def test_retry_claim_resumes_failed_attempt_without_changing_segment(
    too_early, missing_parent
):
    """配送重複は期限前に no-op、期限後は失敗した原 Session だけを継承する。"""
    run = create_queued_run()
    run.status = "RETRY_PENDING"
    run.error_json = {
        "code": MODEL_CAPACITY_CODE,
        "retryable": True,
        "retry_at": (datetime.now(UTC) + timedelta(seconds=60 if too_early else -1)).isoformat(),
    }
    segment = create_segment(run, status=RunSegmentStatus.RUNNING)
    segment.checkpoint_json = {"summary": "Keep the original checkpoint"}
    parent = AgentSession(id=uuid4(), run_id=run.id, run_attempt_id=uuid4(), sdk_session_id=uuid4())
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(
        side_effect=[
            scalar_result(run),
            scalar_result(segment),
            scalar_result(None if missing_parent else parent),
        ]
    )
    session.scalar = AsyncMock(side_effect=[None, 2, 20])
    call = RunRepository(session).claim_for_execution(
        run.id,
        worker_id="worker",
        lease_token="test-token",
        lease_token_hash="a" * 64,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        max_attempts=3,
    )
    if missing_parent:
        with pytest.raises(LeaseValidationError, match="original AgentSession"):
            await call
        return
    claimed = await call
    if too_early:
        assert claimed is None
        session.add_all.assert_not_called()
    else:
        assert claimed.continuation_mode is SessionContinuationMode.RESUME
        assert claimed.parent_sdk_session_id == parent.sdk_session_id
        assert claimed.parent_run_attempt_id == parent.run_attempt_id
        assert claimed.parent_agent_session_id == parent.id
        assert claimed.run_segment_id == segment.id and claimed.attempt_no == 2
        assert claimed.checkpoint_json == segment.checkpoint_json
        assert segment.continuation_mode == "INITIAL" and segment.parent_agent_session_id is None
        assert run.error_json is None
