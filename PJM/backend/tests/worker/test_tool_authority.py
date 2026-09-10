"""Worker 所有 scope の隔離、子 task 継承、終了時失効を実装経路で検証する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from projectmind.agent.domain import (
    AgentEvent,
    AgentEventType,
    AgentSessionRef,
    EngineHealth,
    EngineHealthStatus,
    ForkContext,
    ResumeContext,
    RunContext,
)
from projectmind.agent.result_validation import ResultValidator
from projectmind.runs.domain import LeaseValidationError, SessionContinuationMode
from projectmind.worker.executor import AgentRunExecutor
from projectmind.worker.tool_authority import (
    ToolExecutionAuthority,
    bind_tool_authority,
    require_tool_authority,
)
from tests.agent.test_result_validation import MemoryEvidenceLookup
from tests.worker.test_agent_run_executor import (
    ContextBuilder,
    _claimed,
    _context,
    _event,
    _service,
)


@pytest.mark.parametrize("field", ["run_id", "run_attempt_id", "project_id", "user_id"])
def test_binding_rejects_each_foreign_context_identity(tmp_path: Path, field: str) -> None:
    """原 claim を持つだけでは別 Run/Attempt/Project/actor の runtime を作れない。"""

    claimed = _claimed()
    context = _context(claimed, tmp_path, 1)
    foreign = {
        "run_id": replace(context, run_id=uuid4()),
        "run_attempt_id": replace(context, run_attempt_id=uuid4()),
        "project_id": replace(context, project_id=uuid4()),
        "user_id": replace(context, user_id=uuid4()),
    }[field]
    with pytest.raises(LeaseValidationError), bind_tool_authority(claimed):
        require_tool_authority(foreign)
    with pytest.raises(LeaseValidationError):
        require_tool_authority(context)


def test_nested_scope_restores_outer_and_revokes_only_inner(tmp_path: Path) -> None:
    """内側 scope 終了で外側の原権限を失わず、捕捉済み内側 callback は失効する。"""

    outer = _claimed()
    inner = _claimed()
    with bind_tool_authority(outer):
        first = require_tool_authority(_context(outer, tmp_path, 1))
        with bind_tool_authority(inner):
            second = require_tool_authority(_context(inner, tmp_path, 1))
            assert second.claimed is inner
        with pytest.raises(LeaseValidationError):
            second.require_active()
        assert require_tool_authority(_context(outer, tmp_path, 1)) is first
    with pytest.raises(LeaseValidationError):
        first.require_active()
    assert outer.lease_token not in repr(first)


@pytest.mark.asyncio
async def test_concurrent_scopes_and_child_tasks_keep_original_claim(tmp_path: Path) -> None:
    """同じ Engine を使う並行 Run と子分析の Context を取り違えない。"""

    ready = asyncio.Event()
    count = 0

    async def execute_one() -> ToolExecutionAuthority:
        """二つの所有 task を同時に生存させ、相手の Context を借りないことを確かめる。"""

        nonlocal count
        claimed = _claimed()
        context = _context(claimed, tmp_path, 1)
        with bind_tool_authority(claimed):
            authority = require_tool_authority(context)
            count += 1
            if count == 2:
                ready.set()
            await ready.wait()

            async def child() -> ToolExecutionAuthority:
                """子 task に継承された同じ scope を返す。"""

                await asyncio.sleep(0)
                return require_tool_authority(context)

            assert await asyncio.create_task(child()) is authority
            assert authority.claimed is claimed
            return authority

    first, second = await asyncio.gather(execute_one(), execute_one())
    assert first is not second
    for authority in (first, second):
        with pytest.raises(LeaseValidationError):
            authority.require_active()


@pytest.mark.asyncio
async def test_orphan_inherited_context_is_revoked_when_owner_exits(tmp_path: Path) -> None:
    """ContextVar のコピーを持つ孤立 task も、親 scope 終了後は新しい権限を得ない。"""

    claimed = _claimed()
    context = _context(claimed, tmp_path, 1)
    release = asyncio.Event()

    async def orphan() -> None:
        """親の終了まで待ってから、継承 Context からの発行を試す。"""

        await release.wait()
        with pytest.raises(LeaseValidationError):
            require_tool_authority(context)

    with bind_tool_authority(claimed):
        task = asyncio.create_task(orphan())
    release.set()
    await task


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [
    SessionContinuationMode.INITIAL, SessionContinuationMode.REPLACE,
    SessionContinuationMode.RESUME, SessionContinuationMode.FORK,
])
@pytest.mark.parametrize("terminal_failure", [False, True])
async def test_real_executor_binds_lazy_stream_and_close_without_leaking_authority(
    tmp_path: Path, mode: SessionContinuationMode, terminal_failure: bool,
) -> None:
    """anext が別 task でも close まで同じ権限を保ち、終態保存失敗でも必ず失効する。"""

    claimed = replace(
        _claimed(), continuation_mode=mode, parent_run_attempt_id=uuid4(),
        parent_sdk_session_id=uuid4(), parent_agent_session_id=uuid4(),
    )
    captured: list[ToolExecutionAuthority] = []
    operations: list[str] = []
    session_id = str(uuid4())
    observed_context: list[RunContext] = []

    class Engine:
        """SDK を起動せず、実 Executor の遅延 stream と finally を観測する。"""

        async def stream(self, context: RunContext) -> AsyncIterator[AgentEvent]:
            """別 task の anext と元所有 task の close が同じ権限を見ることを確かめる。"""

            observed_context.append(context)
            authority = require_tool_authority(context)
            captured.append(authority)
            try:
                yield _event(claimed, 10, AgentEventType.SESSION_STARTED, session_id=session_id)
                assert require_tool_authority(context) is authority

                async def child() -> ToolExecutionAuthority:
                    """分岐分析と同じ task 継承で親の scope を観測する。"""

                    return require_tool_authority(context)

                assert await asyncio.create_task(child()) is authority
                yield _event(
                    claimed, 11, AgentEventType.RESULT_COMPLETED, session_id=session_id,
                    payload={"structured_output": {"summary": "completed"}},
                )
            finally:
                captured.append(require_tool_authority(context))

        def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
            """INITIAL/REPLACE の開始経路を記録する。"""

            operations.append("execute")
            return self.stream(context)

        def resume(self, context: ResumeContext) -> AsyncIterator[AgentEvent]:
            """RESUME でも現在 RunContext を用いて権限を照合する。"""

            operations.append("resume")
            return self.stream(context.run)

        def fork(self, context: ForkContext) -> AsyncIterator[AgentEvent]:
            """FORK の新 SDK session も現在 RunContext を使う。"""

            operations.append("fork")
            return self.stream(context.run)

        async def interrupt(self, session: AgentSessionRef) -> None:
            """この正常 stream の試験で予期しない中断を隠さない。"""

            raise AssertionError("Unexpected interruption")

        async def health(self) -> EngineHealth:
            """実サービスへ問い合わせない利用可能な Engine の test double。"""

            return EngineHealth(
                status=EngineHealthStatus.AVAILABLE, engine="test", sdk_version="test",
                cli_version="test",
            )

    service = _service()
    if terminal_failure:
        service.finalize_execution.side_effect = LeaseValidationError("terminal commit failed")
    executor = AgentRunExecutor(
        run_service=service, context_builder=ContextBuilder(tmp_path), engine=Engine(),
        result_validator=ResultValidator(MemoryEvidenceLookup(frozenset())), lease_seconds=60,
    )
    done = asyncio.Event()
    session_ref: asyncio.Future[AgentSessionRef] = asyncio.get_running_loop().create_future()
    if terminal_failure:
        with pytest.raises(LeaseValidationError):
            await executor._execute_claimed(claimed, done, asyncio.Event(), session_ref)
    else:
        await executor._execute_claimed(claimed, done, asyncio.Event(), session_ref)
    assert done.is_set()
    assert len(captured) == 2 and captured[0] is captured[1]
    assert captured[0].claimed is claimed
    assert operations == [{
        SessionContinuationMode.INITIAL: "execute", SessionContinuationMode.REPLACE: "execute",
        SessionContinuationMode.RESUME: "resume", SessionContinuationMode.FORK: "fork",
    }[mode]]
    with pytest.raises(LeaseValidationError):
        captured[0].require_active()
    with pytest.raises(LeaseValidationError):
        require_tool_authority(observed_context[0])
    assert not hasattr(observed_context[0], "lease_token")
    assert not hasattr(observed_context[0], "tool_authority")
