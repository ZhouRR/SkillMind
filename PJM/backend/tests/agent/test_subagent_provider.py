"""扇出 Provider の並行性・受限派生・失敗隔離を検証する (計画 §23 P2/D1/D4/D6)。

engine は fake で置く。ここで確かめたいのは model の振る舞いではなく**平台が子へ何を渡すか**
——渡す能力・渡す予算・失敗をどこで止めるか——であり、それは全部確定的に検査できる。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from projectmind.agent.domain import (
    AgentEvent,
    AgentEventType,
    RegisteredTool,
    RunContext,
    RunLimits,
    RunWorkspace,
)
from projectmind.agent.subagent_provider import SubagentDispatchProvider
from projectmind.agent.subagent_sessions import SubagentSessionDraft
from projectmind.agent.tool_gateway import RunToolContext, ToolProviderError

_RUN_ID = uuid4()


def _tool(capability: str) -> RegisteredTool:
    """登録済み Tool の最小形を作る。"""

    return RegisteredTool(
        capability=capability,
        sdk_name=f"mcp__projectmind__{capability.replace('/', '_').replace('.', '_')}",
        provider="platform",
        integration_id=None,
        input_schema={"type": "object"},
    )


def _parent_context(tmp_path: Path, *, max_turns: int = 20) -> RunContext:
    """扇出元の Run context を作る。"""

    return RunContext(
        run_id=_RUN_ID,
        run_attempt_id=uuid4(),
        project_id=uuid4(),
        user_id=uuid4(),
        prompt="main objective",
        task_snapshot={},
        skill_snapshots=(),
        resolved_sources={},
        permission_snapshot={
            "allowed_capabilities": [
                "workspace.read/v1",
                "workspace.search/v1",
                "workspace.write/v1",
                "issue.read/v1",
                "change.propose/v1",
                "interaction.request/v1",
                "subagent.dispatch/v1",
            ]
        },
        workspace=RunWorkspace(
            root=tmp_path,
            cwd=tmp_path / "workspace",
            input_dir=tmp_path / "input",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "tmp",
        ),
        limits=RunLimits(max_turns=max_turns, wall_timeout_seconds=900, max_output_bytes=1_048_576),
        result_schema={},
        tools=(
            _tool("workspace.read/v1"),
            _tool("workspace.search/v1"),
            _tool("workspace.write/v1"),
            _tool("issue.read/v1"),
            _tool("change.propose/v1"),
            _tool("subagent.dispatch/v1"),
        ),
        model="test-model",
    )


def _tool_context(parent: RunContext) -> RunToolContext:
    """Provider へ渡す Tool context を作る。"""

    return RunToolContext(
        run_id=parent.run_id,
        run_attempt_id=parent.run_attempt_id,
        project_id=parent.project_id,
        user_id=parent.user_id,
        tool=_tool("subagent.dispatch/v1"),
        workspace=parent.workspace,
        run=parent,
    )


class RecordingEngine:
    """子 context を記録し、固定の text を返す fake engine。"""

    def __init__(self, *, delay: float = 0.0) -> None:
        """観測した子 context と、並行性を測るための遅延を保持する。"""

        self.contexts: list[RunContext] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._delay = delay

    def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """子 context を記録して text event を一つ流す。"""

        self.contexts.append(context)

        async def stream() -> AsyncIterator[AgentEvent]:
            """並行数を数えながら text event を一つ流す。"""

            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            try:
                if self._delay:
                    await asyncio.sleep(self._delay)
                yield AgentEvent(
                    run_id=context.run_id,
                    run_attempt_id=context.run_attempt_id,
                    agent_session_id=str(uuid4()),
                    sequence=1,
                    occurred_at=datetime.now(UTC),
                    event_type=AgentEventType.TEXT_COMPLETED,
                    payload={"text": f"result {context.permission_snapshot['subagent_branch']}"},
                )
            finally:
                self.concurrent -= 1

        return stream()


class FailingEngine:
    """指定 branch だけ失敗させる fake engine。"""

    def __init__(self, failing_key: str) -> None:
        """落とす branch の key を保持する。"""

        self.failing_key = failing_key

    def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """該当 branch で例外を送出し、それ以外は正常に返す。"""

        key = context.permission_snapshot["subagent_branch"]

        async def stream() -> AsyncIterator[AgentEvent]:
            """failing_key の branch だけ例外を、他は event を流す。"""

            if key == self.failing_key:
                raise RuntimeError("branch blew up")
            yield AgentEvent(
                run_id=context.run_id,
                run_attempt_id=context.run_attempt_id,
                agent_session_id=str(uuid4()),
                sequence=1,
                occurred_at=datetime.now(UTC),
                event_type=AgentEventType.TEXT_COMPLETED,
                payload={"text": f"ok {key}"},
            )

        return stream()


def _request(*keys: str, capabilities: list[str] | None = None) -> dict[str, object]:
    """branch key から dispatch request を組み立てる。"""

    return {
        "objective": "check independent aspects",
        "branches": [
            {
                "key": key,
                "objective": f"analyse {key}",
                **({"capabilities": capabilities} if capabilities else {}),
            }
            for key in keys
        ],
    }


@pytest.mark.asyncio
async def test_branches_run_concurrently(tmp_path: Path) -> None:
    """扇出の要件そのもの——branch が並行に走ることを確認する。"""

    engine = RecordingEngine(delay=0.05)
    provider = SubagentDispatchProvider(engine=lambda: engine)
    parent = _parent_context(tmp_path)

    await provider.execute(_tool_context(parent), _request("a", "b", "c"))

    assert engine.max_concurrent == 3


@pytest.mark.asyncio
async def test_child_never_receives_write_effect_or_interaction_capabilities(
    tmp_path: Path,
) -> None:
    """子 context の能力集が主の読み取り真部分集合であることを確認する (§23 D1)。

    これが §23 の安全性の全部。緩むと外部効果の帰属と workspace 書き込みの衝突が復活する。
    """

    engine = RecordingEngine()
    provider = SubagentDispatchProvider(engine=lambda: engine)
    parent = _parent_context(tmp_path)

    await provider.execute(_tool_context(parent), _request("a"))

    granted = set(engine.contexts[0].permission_snapshot["allowed_capabilities"])
    assert granted == {"workspace.read/v1", "workspace.search/v1", "issue.read/v1"}
    # Tool 一覧も同時に絞る。能力集だけ絞って tool を残すと SDK 側から呼べてしまう。
    assert {tool.capability for tool in engine.contexts[0].tools} == granted


@pytest.mark.asyncio
async def test_child_cannot_fan_out_again(tmp_path: Path) -> None:
    """子は扇出能力を持たない (§23 D5)。入れ子は予算計算を破綻させる。"""

    engine = RecordingEngine()
    provider = SubagentDispatchProvider(engine=lambda: engine)

    await provider.execute(_tool_context(_parent_context(tmp_path)), _request("a"))

    assert "subagent.dispatch/v1" not in engine.contexts[0].permission_snapshot[
        "allowed_capabilities"
    ]


@pytest.mark.asyncio
async def test_budget_is_split_not_multiplied(tmp_path: Path) -> None:
    """子の合計予算が Run 上限を超えないことを確認する (§23 D4)。"""

    engine = RecordingEngine()
    provider = SubagentDispatchProvider(engine=lambda: engine)
    parent = _parent_context(tmp_path, max_turns=20)

    result = await provider.execute(_tool_context(parent), _request("a", "b", "c", "d"))

    per_branch = result.response["budget"]["turns_per_branch"]
    assert per_branch == 5
    assert per_branch * 4 <= parent.limits.max_turns
    assert all(context.limits.max_turns == per_branch for context in engine.contexts)


@pytest.mark.asyncio
async def test_one_failing_branch_does_not_abort_the_group(tmp_path: Path) -> None:
    """一路の失敗を値として返し、他路の結論は保つ (§23 D6)。"""

    provider = SubagentDispatchProvider(engine=lambda: FailingEngine("b"))

    result = await provider.execute(
        _tool_context(_parent_context(tmp_path)), _request("a", "b", "c")
    )

    outcomes = {item["key"]: item["outcome"] for item in result.response["results"]}
    assert outcomes == {"a": "COMPLETED", "b": "FAILED", "c": "COMPLETED"}
    failed = next(item for item in result.response["results"] if item["key"] == "b")
    assert failed["failure_code"] == "branch_failed"
    # 内部例外の型名や本文は主 Agent の context へ流さない。
    assert failed["summary"] == ""


@pytest.mark.asyncio
async def test_a_stalled_branch_is_cut_off_without_blocking_the_others(
    tmp_path: Path,
) -> None:
    """停滞した branch は打ち切る。Run の wall timeout を一路が食い潰さない (§23 D6)。"""

    class StallingEngine:
        """decide しない branch を再現する fake。"""

        def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
            """終わらない stream を返す。"""

            async def stream() -> AsyncIterator[AgentEvent]:
                """終わらない event 待ちを再現する。"""

                await asyncio.sleep(10)
                yield AgentEvent(
                    run_id=context.run_id,
                    run_attempt_id=context.run_attempt_id,
                    agent_session_id=str(uuid4()),
                    sequence=1,
                    occurred_at=datetime.now(UTC),
                    event_type=AgentEventType.TEXT_COMPLETED,
                    payload={"text": "never"},
                )

            return stream()

    provider = SubagentDispatchProvider(
        engine=lambda: StallingEngine(), branch_timeout_seconds=0.05
    )

    result = await provider.execute(_tool_context(_parent_context(tmp_path)), _request("a"))

    assert result.response["results"][0]["outcome"] == "TIMED_OUT"
    assert result.response["results"][0]["failure_code"] == "branch_timed_out"


@pytest.mark.asyncio
async def test_requesting_a_forbidden_capability_is_refused(tmp_path: Path) -> None:
    """禁止能力の明示要求は Provider error になる。黙って削らない。"""

    provider = SubagentDispatchProvider(engine=lambda: RecordingEngine())

    with pytest.raises(ToolProviderError) as error:
        await provider.execute(
            _tool_context(_parent_context(tmp_path)),
            _request("a", capabilities=["workspace.write/v1"]),
        )

    assert error.value.code == "capability_not_granted"


@pytest.mark.asyncio
async def test_duplicate_branch_keys_are_refused(tmp_path: Path) -> None:
    """同じ key が二つあると主 Agent が結果を取り違える。"""

    provider = SubagentDispatchProvider(engine=lambda: RecordingEngine())

    with pytest.raises(ToolProviderError) as error:
        await provider.execute(_tool_context(_parent_context(tmp_path)), _request("a", "a"))

    assert error.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_dispatch_without_a_frozen_run_context_is_refused(tmp_path: Path) -> None:
    """主 context 無しでは広い権限で走らせず閉じる。"""

    provider = SubagentDispatchProvider(engine=lambda: RecordingEngine())
    parent = _parent_context(tmp_path)
    context = RunToolContext(
        run_id=parent.run_id,
        run_attempt_id=parent.run_attempt_id,
        project_id=parent.project_id,
        user_id=parent.user_id,
        tool=_tool("subagent.dispatch/v1"),
        workspace=parent.workspace,
    )

    with pytest.raises(ToolProviderError) as error:
        await provider.execute(context, _request("a"))

    assert error.value.code == "unavailable"


@pytest.mark.asyncio
async def test_dispatch_produces_evidence_naming_every_branch(tmp_path: Path) -> None:
    """扇出は主 Session 上の一つの Evidence へ収斂する (§23 D3)。"""

    provider = SubagentDispatchProvider(engine=lambda: RecordingEngine())

    result = await provider.execute(
        _tool_context(_parent_context(tmp_path)), _request("a", "b")
    )

    assert len(result.evidence) == 1
    excerpt = result.evidence[0].excerpt or ""
    assert "a: COMPLETED" in excerpt
    assert "b: COMPLETED" in excerpt


class _RecordingRecorder:
    """保存要求を記録し、決定的な agent_session_id を返す fake recorder。"""

    def __init__(self, *, fail: bool = False) -> None:
        """観測した draft と、保存失敗を再現するかどうかを保持する。"""

        self.drafts: list[SubagentSessionDraft] = []
        self.calls = 0
        self._fail = fail

    async def record_branches(
        self,
        *,
        run_id: UUID,
        run_attempt_id: UUID,
        drafts: Sequence[SubagentSessionDraft],
    ) -> tuple[UUID, ...]:
        """一組の branch をまとめて受け取り、採番済み ID を返す。"""

        self.calls += 1
        if self._fail:
            raise RuntimeError("recorder is down")
        self.drafts.extend(drafts)
        return tuple(uuid4() for _ in drafts)


@pytest.mark.asyncio
async def test_result_reports_a_persisted_session_id(tmp_path: Path) -> None:
    """response の agent_session_id が実際に保存された行を指すことを確認する (§23 P3b)。

    以前は branch ごとに uuid4 を生成して返しており、監査でどこからも引けない参照だった。
    引けない ID は「追跡できる」と誤読させるぶん、無い方がまだ良い。
    """

    recorder = _RecordingRecorder()
    provider = SubagentDispatchProvider(engine=lambda: RecordingEngine(), session_recorder=recorder)

    result = await provider.execute(
        _tool_context(_parent_context(tmp_path)), _request("a", "b")
    )

    returned = [item["agent_session_id"] for item in result.response["results"]]
    assert len(set(returned)) == 2
    assert [draft.branch_key for draft in recorder.drafts] == ["a", "b"]


@pytest.mark.asyncio
async def test_all_branches_are_persisted_in_one_call(tmp_path: Path) -> None:
    """一組の branch を一回でまとめて保存する。

    部分的に書けると、失敗した路だけが欠けた記録が残り「二つの面しか見ていない」が
    「二つの面は全部見た」と読める——P3a で潰したのと同じ誤読を保存側で作らない。
    """

    recorder = _RecordingRecorder()
    provider = SubagentDispatchProvider(
        engine=lambda: FailingEngine("b"), session_recorder=recorder
    )

    await provider.execute(_tool_context(_parent_context(tmp_path)), _request("a", "b", "c"))

    assert recorder.calls == 1
    assert [draft.outcome for draft in recorder.drafts] == ["COMPLETED", "FAILED", "COMPLETED"]


@pytest.mark.asyncio
async def test_a_failed_branch_keeps_the_sdk_session_it_had_opened(tmp_path: Path) -> None:
    """SDK session が開いた後に落ちた branch は、その本物の ID を保存する。"""

    recorder = _RecordingRecorder()
    provider = SubagentDispatchProvider(
        engine=lambda: _LateFailureEngine("b"), session_recorder=recorder
    )

    await provider.execute(_tool_context(_parent_context(tmp_path)), _request("a", "b"))

    failed = next(draft for draft in recorder.drafts if draft.branch_key == "b")
    assert failed.outcome == "FAILED"
    assert failed.sdk_session_id is not None


@pytest.mark.asyncio
async def test_a_branch_that_never_started_records_no_sdk_session(tmp_path: Path) -> None:
    """SDK が起動する前に落ちた branch は捏造 ID ではなく None を残す。"""

    recorder = _RecordingRecorder()
    provider = SubagentDispatchProvider(
        engine=lambda: FailingEngine("a"), session_recorder=recorder
    )

    await provider.execute(_tool_context(_parent_context(tmp_path)), _request("a"))

    assert recorder.drafts[0].sdk_session_id is None


@pytest.mark.asyncio
async def test_omits_the_session_id_when_persistence_fails(tmp_path: Path) -> None:
    """保存に失敗しても結論は返すが、引けない ID は載せない。

    子の結論は既に得られており、それを捨てる方が高くつく。ただし「引ける」と誤読させる値を
    返すくらいなら、項目ごと落とす。
    """

    provider = SubagentDispatchProvider(
        engine=lambda: RecordingEngine(), session_recorder=_RecordingRecorder(fail=True)
    )

    result = await provider.execute(_tool_context(_parent_context(tmp_path)), _request("a"))

    assert result.response["results"][0]["outcome"] == "COMPLETED"
    assert "agent_session_id" not in result.response["results"][0]
    assert "session" not in result.evidence[0].metadata["branches"][0]


class _LateFailureEngine:
    """session を開いた後に落ちる fake engine。"""

    def __init__(self, failing_key: str) -> None:
        """落とす branch の key を保持する。"""

        self.failing_key = failing_key

    def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """一つ event を流してから、該当 branch だけ例外を送出する。"""

        key = context.permission_snapshot["subagent_branch"]

        async def stream() -> AsyncIterator[AgentEvent]:
            """event を一つ流した後、該当 branch で例外を送出する。"""

            yield AgentEvent(
                run_id=context.run_id,
                run_attempt_id=context.run_attempt_id,
                agent_session_id=str(uuid4()),
                sequence=1,
                occurred_at=datetime.now(UTC),
                event_type=AgentEventType.TEXT_DELTA,
                payload={"text": f"partial {key}"},
            )
            if key == self.failing_key:
                raise RuntimeError("branch blew up mid-stream")

        return stream()
