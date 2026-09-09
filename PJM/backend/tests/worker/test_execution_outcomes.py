"""引擎の停止通知と platform の取消意図を取り違えないことを検証する。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from projectmind.agent.domain import AgentEventType
from projectmind.agent.result_validation import ResultValidator
from projectmind.runs.domain import (
    LeaseValidationError,
    RunAttemptStatus,
    RunCancellationRequestedError,
    RunStatus,
)
from projectmind.worker.executor import AgentRunExecutor
from tests.agent.test_result_validation import MemoryEvidenceLookup
from tests.runs.execution_event_fakes import execution_event
from tests.worker.test_agent_run_executor import (
    ContextBuilder,
    SequenceEngine,
    _claimed,
    _event,
    _service,
)


@pytest.mark.parametrize(
    "kind, writer",
    [
        (AgentEventType.INTERACTION_REQUESTED, "suspend_for_interaction"),
        (AgentEventType.CHANGE_PROPOSED, "suspend_for_proposal"),
        (AgentEventType.USAGE_UPDATED, "append_agent_event"),
    ],
)
async def test_repository_cancellation_finishes_with_observed_event(
    tmp_path: Path, kind: AgentEventType, writer: str, caplog: pytest.LogCaptureFixture
) -> None:
    """poll が古い false でも DB で確認された取消を扱い、観測用量を失わない。"""

    claimed, service = _claimed(), _service()
    event = execution_event(claimed, kind)
    getattr(service, writer).side_effect = RunCancellationRequestedError("cancel committed")
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine([event]),
        result_validator=ResultValidator(MemoryEvidenceLookup(frozenset())),
        lease_seconds=60,
    )
    await executor.execute(claimed)
    service.finalize_execution.assert_awaited_once()
    final = service.finalize_execution.await_args.kwargs
    assert final["target"] is RunStatus.CANCELLED
    assert final["result"] is None
    assert final["error_json"] is None
    assert final["event"].agent_session_id == event.agent_session_id
    assert final["event"].payload == {
        "reason": "user_interrupted",
        "usage": {"input_tokens": 12},
        "total_cost_usd": 0.02,
    }
    assert not any(record.levelname == "ERROR" for record in caplog.records)


async def test_cancelled_wait_cannot_finalize_after_losing_lease(tmp_path: Path) -> None:
    """待機拒否後の終態 transaction でも lease が失効すれば取消を書き込まない。"""

    claimed, service = _claimed(), _service()
    event = execution_event(claimed, AgentEventType.INTERACTION_REQUESTED)
    service.suspend_for_interaction.side_effect = RunCancellationRequestedError("cancel committed")
    service.finalize_execution.side_effect = LeaseValidationError("lease lost before finalization")
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine([event]),
        result_validator=MagicMock(),
        lease_seconds=60,
    )
    with pytest.raises(LeaseValidationError, match="lease lost"):
        await executor.execute(claimed)
    service.finalize_execution.assert_awaited_once()


@pytest.mark.parametrize("reason", [None, "user_interrupted"])
async def test_interrupted_event_without_durable_intent_is_failed(
    tmp_path: Path, reason: str | None
) -> None:
    """SDK が user を名乗っても、取消意図が無い Run を CANCELLED にしない。"""

    claimed, service = _claimed(), _service()
    event = _event(
        claimed,
        10,
        AgentEventType.SESSION_INTERRUPTED,
        session_id=str(uuid4()),
        payload={} if reason is None else {"reason": reason},
    )
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine([event]),
        result_validator=ResultValidator(MemoryEvidenceLookup(frozenset())),
        lease_seconds=60,
    )
    await executor.execute(claimed)
    final = service.finalize_execution.await_args.kwargs
    assert final["target"] is RunStatus.FAILED
    assert final["attempt_status"] is RunAttemptStatus.FAILED
    assert final["error_json"] == {"code": "agent_session_interrupted", "retryable": False}
    assert final["result"] is None
    assert final["event"] == event
