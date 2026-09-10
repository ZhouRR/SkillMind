"""取消意図と終態化の競争を、実 repository と制御可能な DB 境界で検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.agent.domain import AgentEvent, AgentEventType
from skillmind.db.models import AgentSession, OutboxMessage, RunEvent, RunResult
from skillmind.runs.domain import (
    AgentSessionMetadata,
    LeaseValidationError,
    RunAttemptStatus,
    RunResultRecord,
    RunSegmentStatus,
    RunStatus,
)
from skillmind.runs.repository import RunRepository
from tests.runs.test_execution_gates import execution_rows, row_result


class FinalizationDatabase:
    """SQL の意味で取消/採番を分け、実 ORM 行と lock 順序を観測する。"""

    def __init__(self, *, cancelled: bool) -> None:
        """同じ実行の行と、取り消し後に進んだ event 水位を保持する。"""

        self.claimed, self.run, self.segment, self.attempt = execution_rows()
        now = datetime.now(UTC)
        self.run.row_version = 3
        self.run.started_at = now
        self.run.finished_at = None
        self.run.error_json = None
        self.cancelled = cancelled
        self.primary: AgentSession | None = None
        self.next_sequence = 12 if cancelled else 10
        self.observed: list[str] = []
        self.session = MagicMock(spec=AsyncSession)
        self.session.scalars = AsyncMock(side_effect=self.scalars)
        self.session.scalar = AsyncMock(side_effect=self.scalar)
        self.metadata = AgentSessionMetadata(
            cwd="/runs/example/workspace",
            engine="claude-agent-sdk",
            sdk_version="0.2.110",
            cli_version="2.1.191",
            model="claude-test",
        )
        self.event = AgentEvent(
            run_id=self.run.id,
            run_attempt_id=self.attempt.id,
            agent_session_id=str(uuid4()),
            sequence=10,
            occurred_at=now,
            event_type=AgentEventType.RESULT_COMPLETED,
            payload={
                "structured_output": {"summary": "completed"},
                "num_turns": 3,
                "total_cost_usd": 0.02,
            },
        )
        self.result = RunResultRecord(
            output_schema="sha256:" + "a" * 64,
            result_kind="STRUCTURED_OUTPUT",
            data={"summary": "completed"},
            evidence_refs=(),
            artifact_refs=(),
            change_proposal_refs=(),
            optional_schema_identity={},
            summary="completed",
            confidence=None,
            needs_review=False,
            usage={},
            cost={"total_cost_usd": 0.02},
            validation={"schema_valid": True},
        )

    async def scalars(self, statement: Any) -> MagicMock:
        """table ごとの正しい行だけを返し、Run → Segment → Attempt を記録する。"""

        table = statement.get_final_froms()[0].name
        self.observed.append(table)
        return row_result(
            {
                "runs": self.run,
                "run_segments": self.segment,
                "run_attempts": self.attempt,
                "agent_sessions": self.primary,
            }[table]
        )

    async def scalar(self, statement: Any) -> object:
        """同じ scalar でも取消照会と sequence 採番を別の事実として返す。"""

        if "RUN_CANCEL_REQUESTED" in statement.compile().params.values():
            self.observed.append("cancel")
            return uuid4() if self.cancelled else None
        self.observed.append("sequence")
        return self.next_sequence

    async def finalize(self, **overrides: Any) -> object:
        """通常成功の要求を作り、対象とする競争条件だけを上書きする。"""

        options: dict[str, Any] = {
            "target": RunStatus.SUCCEEDED,
            "attempt_status": RunAttemptStatus.SUCCEEDED,
            "event": self.event,
            "session_metadata": self.metadata,
            "result": self.result,
            "error_json": None,
        }
        options.update(overrides)
        return await RunRepository(self.session).finalize_execution(self.claimed, **options)


@pytest.mark.parametrize("worker_target", [RunStatus.SUCCEEDED, RunStatus.FAILED])
async def test_durable_cancel_wins_inside_terminal_transaction(worker_target: RunStatus) -> None:
    """Worker の古い判定を上書きし、採番衝突なく取消と最後の snapshot を保存する。"""

    database = FinalizationDatabase(cancelled=True)
    failure = worker_target is RunStatus.FAILED
    status = await database.finalize(
        target=worker_target,
        attempt_status=RunAttemptStatus.FAILED if failure else RunAttemptStatus.SUCCEEDED,
        result=None if failure else database.result,
        event=replace(database.event, event_type=AgentEventType.ENGINE_FAILED)
        if failure
        else database.event,
        error_json={"code": "engine_failure"} if failure else None,
    )
    assert status is RunStatus.CANCELLED
    assert database.observed[:4] == ["runs", "run_segments", "run_attempts", "cancel"]
    assert database.run.status == RunStatus.CANCELLED.value
    assert database.segment.status == RunSegmentStatus.CANCELLED.value
    assert database.attempt.status == RunAttemptStatus.CANCELLED.value
    assert database.run.error_json is None
    assert database.attempt.lease_token_hash is None
    added = [call.args[0] for call in database.session.add.call_args_list]
    assert not any(isinstance(item, RunResult) for item in added)
    sessions = [item for item in added if isinstance(item, AgentSession)]
    assert len(sessions) == 1
    assert sessions[0].cost_json == {"total_cost_usd": 0.02}
    models = database.session.add_all.call_args.args[0]
    events = [item for item in models if isinstance(item, RunEvent)]
    assert [item.event_type for item in events] == ["SESSION_INTERRUPTED", "RUN_SNAPSHOT"]
    assert [item.sequence for item in events] == [12, 13]
    assert events[0].payload_json["reason"] == "user_interrupted"
    assert events[0].payload_json["num_turns"] == 3
    assert "structured_output" not in events[0].payload_json
    assert events[-1].payload_json["status"] == "CANCELLED"
    assert len([item for item in models if isinstance(item, OutboxMessage)]) == 2


async def test_cancellation_requires_durable_intent_not_worker_label() -> None:
    """Caller が CANCELLED を指定しても、未記録の user 操作を終態へ作らない。"""

    database = FinalizationDatabase(cancelled=False)
    with pytest.raises(ValueError, match="durable"):
        await database.finalize(
            target=RunStatus.CANCELLED,
            attempt_status=RunAttemptStatus.CANCELLED,
            result=None,
            event=replace(database.event, event_type=AgentEventType.SESSION_INTERRUPTED),
        )
    assert database.run.status == RunStatus.RUNNING.value
    database.session.add_all.assert_not_called()


async def test_lost_lease_cannot_finalize_even_when_cancellation_exists() -> None:
    """取消があっても旧 Worker の書込権は復活せず、意図照会より前に拒否する。"""

    database = FinalizationDatabase(cancelled=True)
    database.attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(LeaseValidationError):
        await database.finalize()
    assert "cancel" not in database.observed
    database.session.add_all.assert_not_called()


@pytest.mark.parametrize("has_event", [False, True])
async def test_finalization_selects_only_its_primary_session(has_event: bool) -> None:
    """同じ Attempt の SUBAGENT が複数いても、主 Session だけを検索して閉じる。"""

    database = FinalizationDatabase(cancelled=False)
    database.primary = AgentSession(
        id=uuid4(),
        run_id=database.run.id,
        run_attempt_id=database.attempt.id,
        sdk_session_id=uuid4(),
        session_kind="PRIMARY",
        status="ACTIVE",
        usage_json={},
        cost_json={},
    )
    event = replace(database.event, agent_session_id=str(database.primary.sdk_session_id))
    await database.finalize(
        target=RunStatus.SUCCEEDED if has_event else RunStatus.FAILED,
        attempt_status=RunAttemptStatus.SUCCEEDED if has_event else RunAttemptStatus.FAILED,
        result=database.result if has_event else None,
        event=event if has_event else None,
        session_metadata=database.metadata if has_event else None,
    )
    statement = database.session.scalars.await_args_list[-1].args[0]
    params = statement.compile().params
    conditions = str(statement.whereclause)
    assert "agent_sessions.session_kind =" in conditions
    assert "agent_sessions.run_id =" in conditions
    assert "agent_sessions.run_attempt_id =" in conditions
    assert set(params.values()) == {database.run.id, database.attempt.id, "PRIMARY"}
    assert "FOR UPDATE" in str(statement)
    assert database.primary.status != "ACTIVE"


async def test_cancel_before_any_session_does_not_fabricate_an_event() -> None:
    """首 event 前は取消 snapshot だけを書き、採番のために Session ID を作らない。"""

    database = FinalizationDatabase(cancelled=True)
    status = await database.finalize(
        target=RunStatus.CANCELLED,
        attempt_status=RunAttemptStatus.CANCELLED,
        event=None,
        session_metadata=None,
        result=None,
    )
    assert status is RunStatus.CANCELLED
    events = [
        item for item in database.session.add_all.call_args.args[0] if isinstance(item, RunEvent)
    ]
    assert len(events) == 1
    assert events[0].event_type == "RUN_SNAPSHOT"
    assert events[0].agent_session_id is None
    assert events[0].sequence == 12
    database.session.add.assert_not_called()


@pytest.mark.parametrize("identity", ["run", "attempt"])
async def test_cancellation_cannot_launder_a_foreign_terminal_event(identity: str) -> None:
    """取消による event 作成し直しでも、別実行の identity を正常化して保存しない。"""

    database = FinalizationDatabase(cancelled=True)
    event = replace(
        database.event,
        **{"run_id" if identity == "run" else "run_attempt_id": uuid4()},
    )
    with pytest.raises(ValueError, match="different RunAttempt"):
        await database.finalize(event=event)
    assert database.run.status == RunStatus.RUNNING.value
    database.session.add.assert_not_called()
