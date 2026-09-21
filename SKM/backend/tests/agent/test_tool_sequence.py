"""実 Gateway の短序列について権限・各子監査・停止・予算を検証する。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from skillmind.agent.tool_gateway import ToolDefinition, ToolRegistry
from skillmind.agent.tool_sequence import ToolSequenceProvider, ToolStepBudget, matches_checks
from tests.agent.test_tool_gateway import (
    CsvIssueProvider,
    MemoryAuditWriter,
    _context,
    _registry,
    _schema,
)


def sequence_runtime(tmp_path, *, provider=None, safe=True, deferred=False, limit=20):
    """実契約と CSV Provider を一 Run の wrapper へ登録する。"""
    provider = provider or CsvIssueProvider()
    original = _registry(provider)
    # fixture registry の公開 definition だけを再利用する。
    issue = ToolDefinition(
        capability="issue.read/v1",
        description="Read fixture issue",
        request_schema=_schema("tools/issue.read/v1/request.schema.json"),
        response_schema=_schema("tools/issue.read/v1/response.schema.json"),
        error_schema=_schema("tools/issue.read/v1/error.schema.json"),
        providers={"csv": provider},
        sequence_safe=safe,
        defer_execution=deferred,
    )
    sequence = ToolDefinition(
        capability="tool.sequence/v1",
        description="Run fixed local/read calls",
        request_schema=_schema("tools/tool.sequence/v1/request.schema.json"),
        response_schema=_schema("tools/tool.sequence/v1/response.schema.json"),
        error_schema=_schema("tools/tool.sequence/v1/error.schema.json"),
        providers={"platform": ToolSequenceProvider()},
        unbound_provider="platform",
        sequence_safe=False,
    )
    registry = ToolRegistry((issue, sequence))
    context = _context(tmp_path, original)
    context = replace(
        context,
        tools=(
            registry.resolve(
                "issue.read/v1", provider="csv", integration_id=context.tools[0].integration_id
            ),
            registry.resolve_unbound("tool.sequence/v1", execution_profile="SUPERVISED"),
        ),
        permission_snapshot={
            "mode": "auto_read_only",
            "allowed_capabilities": ["issue.read/v1", "tool.sequence/v1"],
        },
        limits=replace(context.limits, max_turns=limit),
    )
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    return context, runtime, writer, provider


def step(issue="TICKET-1", **extra):
    """先行結果の式を含まない完全な工具引数を返す。"""
    return {
        "capability": "issue.read/v1",
        "arguments": {"issue_ref": issue, "purpose": "fixture"},
        **extra,
    }


async def invoke(context, runtime, steps, *, call_id="outer-1", session=None):
    """実 SDK callback と同じ予算・認可・Gateway の順序で呼び出す。"""
    arguments = {"steps": steps, "purpose": "fixture sequence"}
    runtime.mcp.on_tool_attempt()
    await runtime.mcp.on_tool_authorized(
        context.tools[1].sdk_name, arguments, call_id, session or str(uuid4())
    )
    result = await runtime.gateway.invoke_mcp(context.tools[1].sdk_name, arguments)
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("count", [1, 3, 5])
async def test_sequence_executes_each_child_through_original_gateway(tmp_path, count):
    """各子は同じ原境界の監査を持ち、全結果保存後に外側へ返される。"""
    context, runtime, writer, provider = sequence_runtime(tmp_path)
    result = await invoke(context, runtime, [step(str(n)) for n in range(count)])
    assert result["outcome"] == "COMPLETED" and result["not_run"] == []
    assert len(writer.completed) == count + 1
    assert provider.calls == count
    assert runtime.gateway.step_budget.used == count + 1
    assert [x["response"]["issue"]["id"] for x in result["steps"]] == list(map(str, range(count)))
    assert len({c.tool_call_id for c in provider.contexts}) == count
    assert all(
        c.run_id == context.run_id and c.run_attempt_id == context.run_attempt_id
        for c in provider.contexts
    )
    assert all(c.tool.integration_id == context.tools[0].integration_id for c in provider.contexts)
    assert all(x["response"]["evidence_refs"] for x in result["steps"])


async def test_outer_replay_returns_original_without_rerunning_children(tmp_path):
    """原 parent 工具の成功再送を、新しい子要求や操作へ変えない。"""
    context, runtime, writer, provider = sequence_runtime(tmp_path)
    session = str(uuid4())
    first = await invoke(context, runtime, [step(), step()], session=session)
    second = await invoke(context, runtime, [step(), step()], session=session)
    assert first == second and provider.calls == 2
    assert len(writer.completed) == 3


@pytest.mark.parametrize("safe,deferred", [(False, False), (True, True)])
async def test_sequence_rejects_non_sequence_and_deferred_tools(tmp_path, safe, deferred):
    """登録済みでも制御や未審査工具を直接実行できない。"""
    context, runtime, _, provider = sequence_runtime(tmp_path, safe=safe, deferred=deferred)
    result = await invoke(context, runtime, [step()])
    assert result["status"] == "error" and provider.calls == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"capability": "change.propose/v1", "arguments": {}},
        {"capability": "subagent.dispatch/v1", "arguments": {}},
        {"capability": "tool.sequence/v1", "arguments": {"steps": [step()], "purpose": "nested"}},
        {"capability": "issue.read/v1", "arguments": {}},
    ],
)
async def test_all_steps_validated_before_first_child(tmp_path, bad):
    """後段が無効なら、前段にも到達せず元の単工具呼出しを変更しない。"""
    context, runtime, _, provider = sequence_runtime(tmp_path)
    result = await invoke(context, runtime, [step(), bad])
    assert result["status"] == "error" and provider.calls == 0


async def test_explicit_condition_stops_even_when_tool_completed(tmp_path):
    """工具成功と後続の許可条件を分け、条件不一致は未実行を残す。"""
    context, runtime, _, provider = sequence_runtime(tmp_path)
    result = await invoke(
        context, runtime, [step(checks=[{"pointer": "/issue/status", "equals": "closed"}]), step()]
    )
    assert result["outcome"] == "STOPPED" and result["stop_reason"] == "condition_failed"
    assert result["not_run"] == [1] and provider.calls == 1


async def test_provider_failure_and_budget_do_not_continue(tmp_path):
    """検証失敗も予算不足も後段を実行しない。外側の一枠を隠さない。"""
    context, runtime, _, provider = sequence_runtime(
        tmp_path, provider=CsvIssueProvider(invalid_response=True)
    )
    result = await invoke(context, runtime, [step(), step()])
    assert result["stop_reason"] == "tool_failed" and provider.calls == 1
    context, runtime, _, provider = sequence_runtime(tmp_path, limit=2)
    result = await invoke(context, runtime, [step(), step(), step()])
    assert result["stop_reason"] == "tool_failed" and provider.calls == 1
    assert runtime.gateway.step_budget.used == 2 and result["not_run"] == [2]


async def test_cancelled_child_propagates_and_never_starts_next(tmp_path):
    """取消を部分成功へ変えず、外側も成功記録しない。"""

    class CancelProvider(CsvIssueProvider):
        """外部 I/O の取消を注入する Provider。"""

        async def execute(self, context, arguments):
            """一度目で取消し、後段呼出し数を検証する。"""
            self.calls += 1
            raise asyncio.CancelledError()

    context, runtime, writer, provider = sequence_runtime(tmp_path, provider=CancelProvider())
    with pytest.raises(asyncio.CancelledError):
        await invoke(context, runtime, [step(), step()])
    assert provider.calls == 1 and writer.completed == []


@pytest.mark.parametrize(
    "value,checks,expected",
    [
        ({"x": True}, [{"pointer": "/x", "equals": 1}], False),
        ({"x": "hallo\r"}, [{"pointer": "/x", "equals": "hallo"}], False),
        ({"a/b": {"~x": ["v"]}}, [{"pointer": "/a~1b/~0x/0", "equals": "v"}], True),
        ({"x": []}, [{"pointer": "/x/0", "equals": None}], False),
        ({"x": [0]}, [{"pointer": "/x/00", "equals": 0}], False),
        ({"x": "v"}, [{"pointer": "", "equals": {"x": "v"}}], True),
    ],
)
def test_exact_json_conditions(value, checks, expected):
    """暗黙の型変換や文字列正規化を行わない。"""
    assert matches_checks(value, checks) is expected


def test_shared_budget_fails_without_reset():
    """一度使用した予算は例外後も戻さない。"""
    budget = ToolStepBudget(1)
    budget.consume()
    with pytest.raises(PermissionError):
        budget.consume()
    assert budget.used == 1


def test_new_tool_registrations_do_not_implicitly_allow_sequences():
    """新しい Provider は明示登録されるまで序列へ入れない。"""
    from skillmind.agent.contract_store import ContractStore
    from skillmind.agent.tool_catalog import _tool_definition

    definition = _tool_definition(
        ContractStore(Path(__file__).resolve().parents[3] / "contracts"),
        capability="issue.read/v1",
        description="fixture",
        providers={"csv": CsvIssueProvider()},
    )
    assert definition.sequence_safe is False


async def test_revoked_dispatch_stops_before_next_provider_call(tmp_path):
    """静的検査後の撤権は各子の dispatch 検査で止める。"""
    context, runtime, writer, provider = sequence_runtime(tmp_path)
    original = writer.verify_dispatch

    async def verify(lease):
        """第一子完了後の実権限失効を注入する。"""
        await original(lease)
        if provider.calls:
            raise PermissionError("revoked")

    writer.verify_dispatch = verify
    result = await invoke(context, runtime, [step(), step(), step()])
    assert provider.calls == 1
    assert result["outcome"] == "STOPPED" and result["not_run"] == [2]


async def test_unconfirmed_outer_result_never_replays_children(tmp_path):
    """外側の成功 commit 応答未知で同一要求が再来しても子を再送しない。"""
    context, runtime, writer, provider = sequence_runtime(tmp_path)
    original = writer.complete

    async def complete(lease, **kwargs):
        """子は確定し、外側だけ未確認のままにする。"""
        if lease.invocation.tool.capability == "tool.sequence/v1":
            raise OSError("unconfirmed")
        return await original(lease, **kwargs)

    writer.complete = complete
    session = str(uuid4())
    first = await invoke(context, runtime, [step(), step()], session=session)
    second = await invoke(context, runtime, [step(), step()], session=session)
    assert first["status"] == second["status"] == "error"
    assert provider.calls == 2 and len(writer.completed) == 2


async def test_identical_steps_are_distinct_operations_not_deduplicated(tmp_path):
    """同じ引数でも異なる position は別の原操作として記録する。"""
    context, runtime, writer, provider = sequence_runtime(tmp_path)
    result = await invoke(context, runtime, [step(), step(), step()])
    assert result["outcome"] == "COMPLETED" and provider.calls == 3
    assert len({str(c.tool_call_id) for c in provider.contexts}) == 3


@pytest.mark.parametrize("pointer", ["/x~2", "x", "/x~"])
def test_malformed_pointer_is_not_an_alternative_field_spelling(pointer):
    """不正な escape を実在 field の別表記として扱わない。"""
    assert not matches_checks({"x~2": True, "x~": True}, [{"pointer": pointer, "equals": True}])
