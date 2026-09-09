"""待機/通常 event が持久取消を迂回しないことを実 repository で検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from projectmind.agent.domain import AgentEventType
from projectmind.effects.proposal import parse_change_proposal_request
from projectmind.runs.domain import LeaseValidationError, RunCancellationRequestedError, RunStatus
from projectmind.runs.interaction import parse_interaction_request
from projectmind.runs.repository import RunRepository
from projectmind.runs.service import RunService
from tests.runs.execution_event_fakes import execution_event, persist_execution_event
from tests.runs.test_effect_decision_service import RecordingTransaction
from tests.runs.test_execution_finalization import FinalizationDatabase

KINDS = (
    AgentEventType.INTERACTION_REQUESTED,
    AgentEventType.CHANGE_PROPOSED,
    AgentEventType.USAGE_UPDATED,
)


@pytest.mark.parametrize("kind", KINDS[:2])
async def test_service_rolls_back_rejected_wait_before_propagating_cancellation(
    kind: AgentEventType,
) -> None:
    """持久取消の拒否を transaction 内で握り潰して commit しない。"""

    database = FinalizationDatabase(cancelled=True)
    transaction = RecordingTransaction()
    database.session.__aenter__.return_value = database.session
    database.session.begin.return_value = transaction
    service = RunService(MagicMock(return_value=database.session))
    event = execution_event(database.claimed, kind)
    with pytest.raises(RunCancellationRequestedError):
        if kind is AgentEventType.INTERACTION_REQUESTED:
            await service.suspend_for_interaction(
                database.claimed,
                event=event,
                session_metadata=database.metadata,
                interaction=parse_interaction_request(event.payload["interaction_request"]),
            )
        else:
            await service.suspend_for_proposal(
                database.claimed,
                event=event,
                session_metadata=database.metadata,
                proposal=parse_change_proposal_request(event.payload["change_proposal_request"]),
            )
    assert transaction.exception_type is RunCancellationRequestedError
    database.session.add_all.assert_not_called()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("sequence", [10, 20])
async def test_cancelled_execution_rejects_waits_and_events_before_any_write(
    kind: AgentEventType, sequence: int
) -> None:
    """採番の衝突/非衝突の双方で取消を優先し、待機・Session・Effect を新設しない。"""

    database = FinalizationDatabase(cancelled=True)
    event = execution_event(database.claimed, kind, sequence=sequence)
    with pytest.raises(RunCancellationRequestedError):
        await persist_execution_event(
            RunRepository(database.session), database.claimed, event, database.metadata
        )
    assert database.observed == ["runs", "run_segments", "run_attempts", "cancel"]
    assert database.run.status == RunStatus.RUNNING.value
    assert database.attempt.lease_token_hash is not None
    database.session.add.assert_not_called()
    database.session.add_all.assert_not_called()
    database.session.flush.assert_not_called()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("invalid", ["lease", "run_event", "attempt_event"])
async def test_cancellation_does_not_hide_invalid_execution_identity(
    kind: AgentEventType, invalid: str
) -> None:
    """取消の存在によって失効 lease や別 Run/Attempt の入力を合法化しない。"""

    database = FinalizationDatabase(cancelled=True)
    event = execution_event(database.claimed, kind)
    if invalid == "lease":
        database.attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif invalid == "run_event":
        event = replace(event, run_id=uuid4())
    else:
        event = replace(event, run_attempt_id=uuid4())
    with pytest.raises(LeaseValidationError if invalid == "lease" else ValueError):
        await persist_execution_event(
            RunRepository(database.session), database.claimed, event, database.metadata
        )
    assert "cancel" not in database.observed
    database.session.add_all.assert_not_called()
