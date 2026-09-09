"""資源準備中の lease、取消、独立期限とモデル開始 gate を検証する。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from projectmind.agent.domain import AgentEvent, AgentEventType, RunContext
from projectmind.runs.domain import (
    ClaimedRun,
    LeaseValidationError,
    RunCancellationRequestedError,
    RunStatus,
)
from projectmind.worker.executor import AgentRunExecutor, _supervisor_exception
from tests.worker.test_agent_run_executor import (
    ContextBuilder,
    SequenceEngine,
    _claimed,
    _context,
    _event,
    _service,
)


class GatedBuilder:
    """準備の開始・解放・中断を時間依存の sleep なしで制御する。"""

    def __init__(self, root: Path, *, cleanup_error: bool = False) -> None:
        """試験ごとに独立した同期点と後片付けの故障注入を用意する。"""

        self.root = root
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = asyncio.Event()
        self.cleanup_error = cleanup_error

    async def build(self, claimed_run: ClaimedRun, *, sequence_start: int) -> RunContext:
        """解放されるまで待機し、中断された場合も終了を観測可能にする。"""

        self.started.set()
        try:
            await self.release.wait()
            return _context(claimed_run, self.root, sequence_start)
        finally:
            self.stopped.set()
            if self.cleanup_error:
                raise RuntimeError("cleanup failed")


@pytest.mark.asyncio
async def test_heartbeat_runs_while_context_is_still_being_prepared(tmp_path: Path) -> None:
    """複数回の heartbeat が無いと準備を解放せず、準備後だけの監視を検出する。"""

    claimed = _claimed()
    service = _service()
    builder = GatedBuilder(tmp_path)

    async def heartbeat(*args: object, **kwargs: object) -> None:
        """準備が進行中であることを確認してから三回目で解放する。"""

        assert args == (claimed.run_attempt_id,)
        assert kwargs == {"lease_token": claimed.lease_token, "lease_seconds": 60}
        assert builder.started.is_set()
        if service.heartbeat_run_attempt.await_count == 3:
            builder.release.set()

    service.heartbeat_run_attempt.side_effect = heartbeat
    engine = SequenceEngine(
        [_event(claimed, 10, AgentEventType.ENGINE_FAILED, session_id=str(uuid4()))]
    )
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=builder,
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
        heartbeat_interval_seconds=0.01,
    )

    await asyncio.wait_for(executor.execute(claimed), timeout=2)

    assert service.heartbeat_run_attempt.await_count >= 3
    service.freeze_agent_task_brief.assert_awaited_once()
    service.verify_execution_start.assert_awaited_once_with(claimed)
    names = [call[0] for call in service.mock_calls]
    assert names.index("freeze_agent_task_brief") < names.index("verify_execution_start")


@pytest.mark.asyncio
async def test_heartbeat_starts_before_prepare_execution_returns(tmp_path: Path) -> None:
    """最初の RUNNING transaction の応答待ちも監視範囲に含める。"""

    claimed, service = _claimed(), _service()
    prepared = service.prepare_execution.return_value
    renewed = asyncio.Event()

    async def prepare(value: ClaimedRun) -> object:
        """最初の heartbeat が動かない旧順序では完了できないようにする。"""

        assert value == claimed
        await renewed.wait()
        return prepared

    async def heartbeat(*args: object, **kwargs: object) -> None:
        """初期 transaction 待ちの同期点を解放する。"""

        renewed.set()

    service.prepare_execution.side_effect = prepare
    service.heartbeat_run_attempt.side_effect = heartbeat
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine([]),
        result_validator=MagicMock(),
        lease_seconds=60,
        heartbeat_interval_seconds=0.01,
    )
    await asyncio.wait_for(executor.execute(claimed), timeout=2)
    assert renewed.is_set()
    service.verify_execution_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_durable_cancellation_stops_preparation_before_brief_and_model(
    tmp_path: Path,
) -> None:
    """Session が無い準備中でも取消が完了し、Brief やモデルを起動しない。"""

    claimed, service, engine = _claimed(), _service(), MagicMock()
    builder = GatedBuilder(tmp_path)
    service.is_cancellation_requested.side_effect = lambda _: builder.started.is_set()
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=builder,
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
        heartbeat_interval_seconds=0.01,
    )

    await asyncio.wait_for(executor.execute(claimed), timeout=2)

    assert builder.stopped.is_set()
    assert not builder.release.is_set()
    service.freeze_agent_task_brief.assert_not_awaited()
    service.verify_execution_start.assert_not_awaited()
    engine.execute.assert_not_called()
    terminal = service.finalize_execution.await_args.kwargs
    assert terminal["target"] is RunStatus.CANCELLED and terminal["event"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_lost_lease_cancels_preparation_without_new_writes(
    tmp_path: Path,
    cleanup_error: bool,
) -> None:
    """後片付けも失敗してよいが、lease 喪失を隠して終態書込を再試行しない。"""

    claimed, service, engine = _claimed(), _service(), MagicMock()
    builder = GatedBuilder(tmp_path, cleanup_error=cleanup_error)

    async def heartbeat(*args: object, **kwargs: object) -> None:
        """準備開始後にのみ lease 喪失を通知する。"""

        await builder.started.wait()
        raise LeaseValidationError("lost lease")

    service.heartbeat_run_attempt.side_effect = heartbeat
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=builder,
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
        heartbeat_interval_seconds=0.01,
    )
    with pytest.raises(LeaseValidationError, match="lost lease"):
        await asyncio.wait_for(executor.execute(claimed), timeout=2)
    assert builder.stopped.is_set()
    service.freeze_agent_task_brief.assert_not_awaited()
    service.verify_execution_start.assert_not_awaited()
    service.finalize_execution.assert_not_awaited()
    engine.execute.assert_not_called()


@pytest.mark.asyncio
async def test_preparation_timeout_does_not_interrupt_terminal_transaction(tmp_path: Path) -> None:
    """準備期限の後も heartbeat を維持し、終態保存自体を同じ期限で中断しない。"""

    claimed, service, engine = _claimed(), _service(), MagicMock()
    builder = GatedBuilder(tmp_path)
    finalizing, renewed = asyncio.Event(), asyncio.Event()

    async def heartbeat(*args: object, **kwargs: object) -> None:
        """準備打切り後の終態 transaction 中にも延長が続くことを示す。"""

        if finalizing.is_set():
            renewed.set()

    async def finalize(*args: object, target: RunStatus, **kwargs: object) -> RunStatus:
        """新しい heartbeat を受けるまで終態 transaction の応答を保留する。"""

        finalizing.set()
        await renewed.wait()
        return target

    service.heartbeat_run_attempt.side_effect = heartbeat
    service.finalize_execution.side_effect = finalize
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=builder,
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
        heartbeat_interval_seconds=0.01,
        preparation_timeout_seconds=0.04,
    )
    await asyncio.wait_for(executor.execute(claimed), timeout=2)
    assert renewed.is_set() and builder.stopped.is_set()
    terminal = service.finalize_execution.await_args.kwargs
    assert terminal["target"] is RunStatus.FAILED
    assert terminal["error_json"]["code"] == "preparation_timeout"
    service.freeze_agent_task_brief.assert_not_awaited()
    engine.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("provider timeout"), LeaseValidationError("stale")])
async def test_builder_errors_keep_their_actual_meaning(tmp_path: Path, error: Exception) -> None:
    """Provider の timeout と準備期限、lease 喪失を同一エラーに畳まない。"""

    claimed, service, engine = _claimed(), _service(), MagicMock()
    builder = MagicMock(build=AsyncMock(side_effect=error))
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=builder,
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
    )
    if isinstance(error, LeaseValidationError):
        with pytest.raises(LeaseValidationError, match="stale"):
            await executor.execute(claimed)
        service.finalize_execution.assert_not_awaited()
    else:
        await executor.execute(claimed)
        assert service.finalize_execution.await_args.kwargs["error_json"] == {
            "code": "context_build_failed",
            "error_type": "TimeoutError",
            "retryable": False,
        }
    engine.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["brief", "start"])
async def test_cancellation_wins_at_each_final_preparation_gate(
    tmp_path: Path, boundary: str
) -> None:
    """直前 poll では未取消でも、DB gate で成立した取消後はモデルを呼ばない。"""

    claimed, service, engine = _claimed(), _service(), MagicMock()
    if boundary == "brief":
        service.freeze_agent_task_brief.side_effect = RunCancellationRequestedError("cancelled")
    else:
        service.verify_execution_start.return_value = False
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
    )
    await executor.execute(claimed)
    assert service.finalize_execution.await_args.kwargs["target"] is RunStatus.CANCELLED
    engine.execute.assert_not_called()


@pytest.mark.asyncio
async def test_start_gate_rejects_a_lease_lost_after_brief_freeze(tmp_path: Path) -> None:
    """Brief 保存成功を、以後も失効しない実行権と誤認しない。"""

    claimed, service, engine = _claimed(), _service(), MagicMock()
    service.verify_execution_start.side_effect = LeaseValidationError("taken over")
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
    )
    with pytest.raises(LeaseValidationError, match="taken over"):
        await executor.execute(claimed)
    service.freeze_agent_task_brief.assert_awaited_once()
    service.finalize_execution.assert_not_awaited()
    engine.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["project_id", "user_id"])
async def test_context_cannot_change_the_claimed_authority(tmp_path: Path, identity: str) -> None:
    """Run ID が同じでも別 Project/actor の Tool context をモデルへ渡さない。"""

    claimed, service, engine = _claimed(), _service(), MagicMock()
    context = replace(_context(claimed, tmp_path, 10), **{identity: uuid4()})
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=MagicMock(build=AsyncMock(return_value=context)),
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
    )
    await executor.execute(claimed)
    service.freeze_agent_task_brief.assert_not_awaited()
    assert (
        service.finalize_execution.await_args.kwargs["error_json"]["code"] == "context_build_failed"
    )
    engine.execute.assert_not_called()


@pytest.mark.asyncio
async def test_worker_shutdown_is_not_a_user_cancellation(tmp_path: Path) -> None:
    """外からの job 取消は伝播し、勝手に user CANCELLED を永続化しない。"""

    claimed, service, engine = _claimed(), _service(), MagicMock()
    builder = GatedBuilder(tmp_path)
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=builder,
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
    )
    execution = asyncio.create_task(executor.execute(claimed))
    try:
        await asyncio.wait_for(builder.started.wait(), timeout=2)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
    assert builder.stopped.is_set()
    service.finalize_execution.assert_not_awaited()
    engine.execute.assert_not_called()


@pytest.mark.asyncio
async def test_late_thread_completion_cannot_resume_cancelled_preparation(tmp_path: Path) -> None:
    """中断できない同期 I/O が後から戻っても、coroutine の完成処理は再開しない。"""

    claimed, service, engine = _claimed(), _service(), MagicMock()
    started, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    completed = AsyncMock()

    def read() -> None:
        """自分専用の同期点だけで制御し、test 終了時に必ず thread を解放する。"""

        try:
            assert release.wait(timeout=3)
        finally:
            finished.set()

    async def build(value: ClaimedRun, *, sequence_start: int) -> RunContext:
        """実物化器と同様、I/O await 後でのみ完成回执へ進む。"""

        started.set()
        await asyncio.to_thread(read)
        await completed()
        return _context(value, tmp_path, sequence_start)

    service.is_cancellation_requested.side_effect = lambda _: started.is_set()
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=MagicMock(build=AsyncMock(side_effect=build)),
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
    )
    try:
        await asyncio.wait_for(executor.execute(claimed), timeout=2)
        assert not finished.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    completed.assert_not_awaited()
    service.freeze_agent_task_brief.assert_not_awaited()
    engine.execute.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_preparation_timeout_must_be_bounded(timeout: float) -> None:
    """ゼロ・負数・非有限値で準備の保護を無効にできない。"""

    with pytest.raises(ValueError, match="Preparation timeout"):
        AgentRunExecutor(
            run_service=MagicMock(),
            context_builder=MagicMock(),
            engine=MagicMock(),
            result_validator=MagicMock(),
            lease_seconds=60,
            preparation_timeout_seconds=timeout,
        )


def test_nested_supervision_error_prioritizes_lost_lease() -> None:
    """発生順ではなく、以後の書込を禁止する根拠を最優先で返す。"""

    lost = LeaseValidationError("stale")
    errors = ExceptionGroup("combined", [ValueError("cleanup"), ExceptionGroup("lease", [lost])])
    assert _supervisor_exception(errors) is lost


@pytest.mark.asyncio
async def test_lost_lease_closes_engine_suspended_between_events(tmp_path: Path) -> None:
    """event 永続化中の lease 喪失でも、次の anext や GC を待たず engine を閉じる。"""

    claimed, service, engine = _claimed(), _service(), MagicMock()
    persisting, closed = asyncio.Event(), asyncio.Event()

    async def events() -> AsyncIterator[AgentEvent]:
        """呼出し元が参照を保持する stream の明示 close を検出する。"""

        try:
            yield _event(claimed, 10, AgentEventType.SESSION_STARTED, session_id=str(uuid4()))
        finally:
            closed.set()

    async def append(*args: object, **kwargs: object) -> None:
        """次 event を要求する前で worker を待機させる。"""

        persisting.set()
        await asyncio.Event().wait()

    async def heartbeat(*args: object, **kwargs: object) -> None:
        """Session が event を返した後にだけ lease を失効させる。"""

        await persisting.wait()
        raise LeaseValidationError("lost while storing")

    stream = events()
    engine.execute.return_value = stream
    service.append_agent_event.side_effect = append
    service.heartbeat_run_attempt.side_effect = heartbeat
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=engine,
        result_validator=MagicMock(),
        lease_seconds=60,
        heartbeat_interval_seconds=0.01,
    )
    with pytest.raises(LeaseValidationError, match="lost while storing"):
        await asyncio.wait_for(executor.execute(claimed), timeout=2)
    assert closed.is_set()
    service.finalize_execution.assert_not_awaited()
