"""AgentRunExecutor の Result 検証、終態 mapping、heartbeat 監督を検証する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from projectmind.agent.domain import (
    AgentEvent,
    AgentEventType,
    AgentSessionRef,
    EngineHealth,
    ForkContext,
    ResumeContext,
    RunContext,
    RunLimits,
    RunWorkspace,
)
from projectmind.agent.result_validation import ResultValidator
from projectmind.runs.domain import (
    ClaimedRun,
    PreparedExecution,
    RunAttemptStatus,
    RunStatus,
    SessionContinuationMode,
)
from projectmind.worker.executor import AgentRunExecutor
from tests.agent.test_result_validation import MemoryEvidenceLookup


def _claimed() -> ClaimedRun:
    """Executor test 用の lease 取得済み Run を返す。"""

    return ClaimedRun(
        run_id=uuid4(),
        run_attempt_id=uuid4(),
        project_id=uuid4(),
        actor_id=uuid4(),
        attempt_no=1,
        lease_token="lease-token",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        row_version=2,
        input_json={},
        task_snapshot_json={"output_schema": "sha256:" + ("c" * 64)},
        permission_snapshot_json={},
        selected_sources_json={},
        limits_snapshot_json={},
    )


def _context(
    claimed: ClaimedRun,
    tmp_path: Path,
    sequence_start: int,
    *,
    wall_timeout_seconds: int = 60,
) -> RunContext:
    """Claim identity と repository sequence を維持した RunContext を返す。"""

    root = tmp_path / str(claimed.run_id)
    return RunContext(
        run_id=claimed.run_id,
        run_attempt_id=claimed.run_attempt_id,
        project_id=claimed.project_id,
        user_id=claimed.actor_id,
        prompt="analyze",
        task_snapshot=claimed.task_snapshot_json,
        skill_snapshots=(),
        resolved_sources={},
        permission_snapshot={"mode": "auto_read_only", "allowed_capabilities": []},
        workspace=RunWorkspace(
            root=root,
            cwd=root / "workspace",
            input_dir=root / "input",
            output_dir=root / "output",
            temp_dir=root / "temp",
        ),
        limits=RunLimits(
            max_turns=5,
            wall_timeout_seconds=wall_timeout_seconds,
            max_output_bytes=4096,
        ),
        result_schema={
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
        tools=(),
        model="claude-test",
        sequence_start=sequence_start,
    )


class ContextBuilder:
    """Prepared sequence をそのまま使う test builder。"""

    def __init__(self, tmp_path: Path, *, wall_timeout_seconds: int = 60) -> None:
        """Workspace root と wall timeout を保持する。"""

        self._tmp_path = tmp_path
        self._wall_timeout_seconds = wall_timeout_seconds

    async def build(self, claimed_run: ClaimedRun, *, sequence_start: int) -> RunContext:
        """ClaimedRun と一致する context を返す。"""

        return _context(
            claimed_run,
            self._tmp_path,
            sequence_start,
            wall_timeout_seconds=self._wall_timeout_seconds,
        )


class _UnsupportedEngineOperations:
    """個別 fake の責務外の SDK 操作を、Protocol の欠落ではなく明示失敗として扱う。"""

    def resume(self, context: ResumeContext) -> AsyncIterator[AgentEvent]:
        """RESUME を実装しない fixture で再開経路が選ばれた場合に失敗させる。"""

        raise AssertionError("Unexpected resume")

    def fork(self, context: ForkContext) -> AsyncIterator[AgentEvent]:
        """FORK 専用 fixture 以外で分岐経路が選ばれた場合に失敗させる。"""

        raise AssertionError("Unexpected fork")

    async def interrupt(self, session_ref: AgentSessionRef) -> None:
        """中断を実装しない fixture が意図せず取消を受け入れないようにする。"""

        raise AssertionError("Unexpected interruption")

    async def health(self) -> EngineHealth:
        """実行 fixture から外部 Engine の診断へ進む経路を許可しない。"""

        raise AssertionError("Unexpected health check")


class HangingEngine(_UnsupportedEngineOperations):
    """最初の event の後、terminal event を返さず待機し続ける test AgentEngine。"""

    def __init__(self, first_event: AgentEvent) -> None:
        """Timeout 前に配送する最初の event を保持する。"""

        self._first_event = first_event

    async def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """一つだけ event を返し、その後は打ち切られるまで待機する。"""

        yield self._first_event
        await asyncio.sleep(3600)


class SequenceEngine(_UnsupportedEngineOperations):
    """固定 AgentEvent 列を返す test AgentEngine。"""

    def __init__(self, events: list[AgentEvent], *, delay: float = 0) -> None:
        """Event と任意 delay を保持する。"""

        self._events = events
        self._delay = delay

    async def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """指定 event を順に返す。"""

        for event in self._events:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield event

    async def interrupt(self, session: AgentSessionRef) -> None:
        """通常 sequence test では interrupt が呼ばれないことを示す。"""

        raise AssertionError(f"Unexpected interrupt: {session}")


class ForkingEngine(_UnsupportedEngineOperations):
    """前 Session を parent として fork し、固定 event 列を返す test engine。"""

    def __init__(self, events: list[AgentEvent]) -> None:
        """Fork 後に返す event と受信 context の記録欄を初期化する。"""

        self._events = events
        self.context: ForkContext | None = None

    async def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """FORK test で新規 Session が誤選択された場合は失敗させる。"""

        del context
        raise AssertionError("Fork continuation must not call execute")
        yield  # pragma: no cover

    async def fork(self, context: ForkContext) -> AsyncIterator[AgentEvent]:
        """監査済み parent context を記録し、Session B の event を返す。"""

        self.context = context
        for event in self._events:
            yield event

    async def interrupt(self, session: AgentSessionRef) -> None:
        """通常完了する fork test では interrupt を許さない。"""

        raise AssertionError(f"Unexpected interrupt: {session}")


class InterruptibleEngine(_UnsupportedEngineOperations):
    """Interrupt 呼出し後に SESSION_INTERRUPTED を返す test engine。"""

    def __init__(self, started: AgentEvent, interrupted: AgentEvent) -> None:
        """開始 event と取消 terminal event を保持する。"""

        self._started = started
        self._interrupted = interrupted
        self._interrupt = asyncio.Event()
        self.session: AgentSessionRef | None = None

    async def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """Session 開始後、interrupt が届くまで stream を維持する。"""

        yield self._started
        await self._interrupt.wait()
        yield self._interrupted

    async def interrupt(self, session: AgentSessionRef) -> None:
        """受け取った session identity を記録して stream を解放する。"""

        self.session = session
        self._interrupt.set()


def _event(
    claimed: ClaimedRun,
    sequence: int,
    event_type: AgentEventType,
    *,
    session_id: str,
    payload: dict[str, object] | None = None,
) -> AgentEvent:
    """Claim identity に一致する AgentEvent を生成する。"""

    return AgentEvent(
        run_id=claimed.run_id,
        run_attempt_id=claimed.run_attempt_id,
        agent_session_id=session_id,
        sequence=sequence,
        occurred_at=datetime.now(UTC),
        event_type=event_type,
        payload=payload or {},
    )


def _service() -> MagicMock:
    """Executor が利用する RunService methods を AsyncMock 化する。"""

    async def finalize(*args: object, target: RunStatus, **kwargs: object) -> RunStatus:
        """競争の無い stub は指定した終態を commit 後の実際の状態として返す。"""

        del args, kwargs
        return target

    service = MagicMock()
    service.prepare_execution = AsyncMock(return_value=PreparedExecution(3, 10))
    service.freeze_agent_task_brief = AsyncMock()
    service.verify_execution_start = AsyncMock(return_value=True)
    service.suspend_for_interaction = AsyncMock(return_value=uuid4())
    service.append_agent_event = AsyncMock()
    service.finalize_execution = AsyncMock(side_effect=finalize)
    service.heartbeat_run_attempt = AsyncMock()
    service.is_cancellation_requested = AsyncMock(return_value=False)
    return service


@pytest.mark.asyncio
async def test_validated_result_reaches_success_terminal(tmp_path: Path) -> None:
    """Result validation 通過時だけ Result record と SUCCEEDED を同時に要求する。"""

    claimed = _claimed()
    session_id = str(uuid4())
    events = [
        _event(claimed, 10, AgentEventType.SESSION_STARTED, session_id=session_id),
        _event(
            claimed,
            11,
            AgentEventType.USAGE_UPDATED,
            session_id=session_id,
            payload={"usage": {"input_tokens": 10}},
        ),
        _event(
            claimed,
            12,
            AgentEventType.RESULT_COMPLETED,
            session_id=session_id,
            payload={"structured_output": {"summary": "completed"}, "total_cost_usd": 0.02},
        ),
    ]
    service = _service()
    validator = ResultValidator(MemoryEvidenceLookup(frozenset()))
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine(events),
        result_validator=validator,
        lease_seconds=60,
    )

    await executor.execute(claimed)

    assert service.append_agent_event.await_count == 2
    terminal = service.finalize_execution.await_args.kwargs
    assert terminal["target"] is RunStatus.SUCCEEDED
    assert terminal["attempt_status"] is RunAttemptStatus.SUCCEEDED
    assert terminal["result"].summary == "completed"
    assert terminal["result"].output_schema == claimed.task_snapshot_json["output_schema"]
    assert terminal["result"].validation["schema_valid"] is True
    assert terminal["result"].usage == {"input_tokens": 10}
    assert terminal["result"].cost == {"total_cost_usd": 0.02}


@pytest.mark.asyncio
async def test_invalid_result_is_finalized_as_failed(tmp_path: Path) -> None:
    """SDK success event でも platform validator 失敗時は FAILED にする。"""

    claimed = _claimed()
    result_event = _event(
        claimed,
        10,
        AgentEventType.RESULT_COMPLETED,
        session_id=str(uuid4()),
        payload={"structured_output": {}},
    )
    service = _service()
    validator = ResultValidator(MemoryEvidenceLookup(frozenset()))
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine([result_event]),
        result_validator=validator,
        lease_seconds=60,
    )

    await executor.execute(claimed)

    terminal = service.finalize_execution.await_args.kwargs
    assert terminal["target"] is RunStatus.FAILED
    assert terminal["event"].event_type is AgentEventType.ENGINE_FAILED
    assert terminal["event"].payload["reason"] == "result_validation_failed"
    assert terminal["error_json"]["code"] == "result_schema_invalid"
    assert terminal["result"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "run_status", "attempt_status"),
    [
        (AgentEventType.ENGINE_FAILED, RunStatus.FAILED, RunAttemptStatus.FAILED),
        (AgentEventType.SESSION_INTERRUPTED, RunStatus.FAILED, RunAttemptStatus.FAILED),
        (
            AgentEventType.SESSION_DEFERRED,
            RunStatus.WAITING_PERMISSION,
            RunAttemptStatus.DEFERRED,
        ),
    ],
)
async def test_terminal_event_mapping(
    tmp_path: Path,
    event_type: AgentEventType,
    run_status: RunStatus,
    attempt_status: RunAttemptStatus,
) -> None:
    """Engine terminal event を対応する Run/Attempt status へ一意に変換する。"""

    claimed = _claimed()
    service = _service()
    validator = MagicMock()
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine([_event(claimed, 10, event_type, session_id=str(uuid4()))]),
        result_validator=validator,
        lease_seconds=60,
    )

    await executor.execute(claimed)

    terminal = service.finalize_execution.await_args.kwargs
    assert terminal["target"] is run_status
    assert terminal["attempt_status"] is attempt_status


@pytest.mark.asyncio
async def test_text_delta_is_not_persisted(tmp_path: Path) -> None:
    """Realtime 専用 TEXT_DELTA を Redis へ送り監査 database へ書き込まない。"""

    claimed = _claimed()
    session_id = str(uuid4())
    service = _service()
    realtime_publisher = MagicMock()
    realtime_publisher.publish = AsyncMock()
    delta = _event(
        claimed,
        10,
        AgentEventType.TEXT_DELTA,
        session_id=session_id,
        payload={"text": "partial"},
    )
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine(
            [
                delta,
                _event(claimed, 11, AgentEventType.ENGINE_FAILED, session_id=session_id),
            ]
        ),
        result_validator=MagicMock(),
        lease_seconds=60,
        realtime_publisher=realtime_publisher,
    )

    await executor.execute(claimed)

    service.append_agent_event.assert_not_awaited()
    realtime_publisher.publish.assert_awaited_once_with(delta)


@pytest.mark.asyncio
async def test_wall_timeout_finalizes_run_as_failed(tmp_path: Path) -> None:
    """Wall timeout 超過時に stream を打ち切り、FAILED(wall_timeout) へ終態化する。"""

    claimed = _claimed()
    session_id = str(uuid4())
    service = _service()
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path, wall_timeout_seconds=1),
        engine=HangingEngine(
            _event(claimed, 10, AgentEventType.SESSION_STARTED, session_id=session_id)
        ),
        result_validator=MagicMock(),
        lease_seconds=60,
    )

    await executor.execute(claimed)

    terminal = service.finalize_execution.await_args.kwargs
    assert terminal["target"] is RunStatus.FAILED
    assert terminal["attempt_status"] is RunAttemptStatus.FAILED
    assert terminal["error_json"] == {"code": "wall_timeout", "retryable": False}
    assert terminal["event"].event_type is AgentEventType.ENGINE_FAILED
    assert terminal["event"].payload == {"reason": "wall_timeout"}
    assert terminal["event"].sequence == 11


@pytest.mark.asyncio
async def test_durable_cancel_intent_interrupts_active_session(tmp_path: Path) -> None:
    """別 process の取消要求が active SDK session を止めて CANCELLED へ閉じる。"""

    claimed = _claimed()
    session_id = str(uuid4())
    service = _service()

    async def cancellation_requested(run_id: object) -> bool:
        """実際に最初の Session event が保存された後だけ取消 intent を見せる。"""

        assert run_id == claimed.run_id
        return bool(service.append_agent_event.await_count > 0)

    service.is_cancellation_requested = AsyncMock(side_effect=cancellation_requested)
    engine = InterruptibleEngine(
        _event(claimed, 10, AgentEventType.SESSION_STARTED, session_id=session_id),
        _event(claimed, 11, AgentEventType.SESSION_INTERRUPTED, session_id=session_id),
    )
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
    )

    await executor.execute(claimed)

    assert engine.session == AgentSessionRef(
        run_id=claimed.run_id,
        run_attempt_id=claimed.run_attempt_id,
        session_id=session_id,
    )
    terminal = service.finalize_execution.await_args.kwargs
    assert terminal["target"] is RunStatus.CANCELLED
    assert terminal["attempt_status"] is RunAttemptStatus.CANCELLED
    assert terminal["event"].event_type is AgentEventType.SESSION_INTERRUPTED


@pytest.mark.asyncio
async def test_context_builder_failure_closes_claimed_run(tmp_path: Path) -> None:
    """Session 作成前の builder failure でも PREPARING/RUNNING を放置しない。"""

    claimed = _claimed()
    service = _service()
    builder = MagicMock()
    builder.build = AsyncMock(side_effect=RuntimeError("secret detail"))
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=builder,
        engine=MagicMock(),
        result_validator=MagicMock(),
        lease_seconds=60,
    )

    await executor.execute(claimed)

    terminal = service.finalize_execution.await_args.kwargs
    assert terminal["event"] is None
    assert terminal["error_json"] == {
        "code": "context_build_failed",
        "error_type": "RuntimeError",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_review_suspends_session_a_and_forks_session_b_to_result(
    tmp_path: Path,
) -> None:
    """Session A の Review 待機後、次 Segment が parent を fork して Result まで進む。"""

    first = replace(_claimed(), run_segment_id=uuid4(), segment_no=1)
    session_a = uuid4()
    interaction_event = _event(
        first,
        10,
        AgentEventType.INTERACTION_REQUESTED,
        session_id=str(session_a),
        payload={
            "interaction_request": {
                "interaction_type": "REVIEW",
                "prompt": "Review the current finding.",
                "rationale": "The final severity requires project context.",
                "impact": "The recommendation may change.",
                "options": [],
                "allow_multiple": False,
                "required": True,
                "expires_in_seconds": 3600,
                "continuation_mode": "FORK",
                "checkpoint": {
                    "summary": "Analysis is ready for review.",
                    "confirmed_facts": ["The affected path exists."],
                    "evidence_refs": [],
                    "artifact_refs": [],
                    "change_proposal_refs": [],
                },
            }
        },
    )
    first_service = _service()
    first_executor = AgentRunExecutor(
        run_service=first_service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine([interaction_event]),
        result_validator=MagicMock(),
        lease_seconds=60,
    )

    await first_executor.execute(first)

    first_service.suspend_for_interaction.assert_awaited_once()
    first_service.finalize_execution.assert_not_awaited()
    suspended = first_service.suspend_for_interaction.await_args.kwargs
    assert suspended["interaction"].continuation_mode is SessionContinuationMode.FORK
    assert suspended["interaction"].checkpoint["summary"] == "Analysis is ready for review."

    parent_attempt_id = first.run_attempt_id
    second = replace(
        _claimed(),
        run_id=first.run_id,
        project_id=first.project_id,
        actor_id=first.actor_id,
        run_segment_id=uuid4(),
        segment_no=2,
        continuation_mode=SessionContinuationMode.FORK,
        parent_agent_session_id=session_a,
        parent_sdk_session_id=session_a,
        parent_run_attempt_id=parent_attempt_id,
        checkpoint_json={"summary": "Analysis is ready for review."},
        checkpoint_checksum="sha256:" + ("7" * 64),
    )
    session_b = uuid4()
    result_event = _event(
        second,
        10,
        AgentEventType.RESULT_COMPLETED,
        session_id=str(session_b),
        payload={"structured_output": {"summary": "Reviewed result"}},
    )
    second_service = _service()
    validator = ResultValidator(MemoryEvidenceLookup(frozenset()))
    fork_engine = ForkingEngine([result_event])
    second_executor = AgentRunExecutor(
        run_service=second_service,
        context_builder=ContextBuilder(tmp_path),
        engine=fork_engine,
        result_validator=validator,
        lease_seconds=60,
    )

    await second_executor.execute(second)

    assert fork_engine.context is not None
    assert fork_engine.context.parent_session == AgentSessionRef(
        run_id=second.run_id,
        run_attempt_id=parent_attempt_id,
        session_id=str(session_a),
    )
    assert second_service.finalize_execution.await_args.kwargs["target"] is RunStatus.SUCCEEDED
    result = second_service.finalize_execution.await_args.kwargs["result"]
    assert result.summary == "Reviewed result"
    assert result.output_schema == second.task_snapshot_json["output_schema"]
