"""実 Gateway の一回限りの実行権と await を跨ぐ候補の固定を検証する。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from projectmind.agent.evidence import EvidenceRecord, ToolAuditLease, ToolInvocation
from projectmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    RunToolRuntime,
)
from tests.agent.test_tool_gateway import (
    CsvIssueProvider,
    MemoryAuditWriter,
    _context,
    _registry,
)


class OwnershipAuditWriter(MemoryAuditWriter):
    """DB を使わず新規認領と保存済み状態を明示する観測用 audit port。"""

    def __init__(self, *, pause: str | None = None) -> None:
        """候補を読む前に停止できる transaction 境界を用意する。"""

        super().__init__()
        self.pause = pause
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.dispatches: list[ToolAuditLease] = []
        self.saved: list[dict[str, Any]] = []
        self.commit_response_lost = False

    async def _checkpoint(self, phase: str) -> None:
        """同じ task 内で待機し、別 task からの入力変更を再現する。"""

        if self.pause == phase:
            self.entered.set()
            await self.release.wait()

    async def start_authorized(self, invocation: ToolInvocation) -> ToolAuditLease:
        """既存 RUNNING は新規実行権でなく未確定の原記録として返す。"""

        await self._checkpoint("start")
        existing = self.by_use_id.get(invocation.sdk_tool_use_id)
        if existing is not None:
            return replace(existing, is_new=False)
        lease = ToolAuditLease(uuid4(), "RUNNING", invocation=invocation, is_new=True)
        self.by_use_id[invocation.sdk_tool_use_id] = lease
        self.invocations[lease.tool_call_id] = invocation
        return lease

    async def verify_dispatch(self, lease: ToolAuditLease) -> None:
        """実 DB fencing の代わりに原 lease を観測し、任意の await を作る。"""

        self.dispatches.append(lease)
        await self._checkpoint("dispatch")

    async def complete(
        self,
        lease: ToolAuditLease,
        *,
        result: dict[str, Any],
        evidence: tuple[EvidenceRecord, ...],
        duration_ms: int,
    ) -> dict[str, Any]:
        """Gateway から渡された候補を待機後に保存し、原結果を再生可能にする。"""

        await self._checkpoint("complete")
        assert duration_ms >= 0
        saved = deepcopy(result)
        self.saved.append(saved)
        self.completed.append((lease.tool_call_id, deepcopy(evidence)))
        invocation = self.invocations[lease.tool_call_id]
        self.by_use_id[invocation.sdk_tool_use_id] = replace(
            lease, status="SUCCEEDED", result=saved, is_new=False
        )
        if self.commit_response_lost:
            raise RuntimeError("Synthetic commit response unavailable")
        return deepcopy(saved)


class PausingIssueProvider(CsvIssueProvider):
    """呼び出し元の可変引数を、制御した await の後で観測する Provider。"""

    def __init__(self, *, pause_first: bool = False) -> None:
        """最初の実行だけを停止し、重複実行の発生も検出する。"""

        super().__init__()
        self.pause_first = pause_first
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.started = 0
        self.observed: list[dict[str, Any]] = []
        self.candidate: ProviderToolResult | None = None

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """実 contract fixture を使い、応答と Evidence の元参照を保持する。"""

        self.started += 1
        if self.pause_first and self.started == 1:
            self.entered.set()
            await self.release.wait()
        self.observed.append(deepcopy(dict(arguments)))
        candidate = await super().execute(context, dict(arguments))
        response = deepcopy(dict(candidate.response))
        response["issue"]["fields"] = {"labels": ["original"]}
        draft = replace(
            candidate.evidence[0],
            source_locator={"sheet": "issues", "rows": [2]},
            metadata={"reproducibility": "snapshot", "labels": ["original"]},
        )
        self.candidate = ProviderToolResult(response=response, evidence=(draft,))
        return self.candidate


def _runtime(
    tmp_path: Path, provider: PausingIssueProvider, writer: OwnershipAuditWriter
) -> tuple[RunToolRuntime, str]:
    """元の versioned Schema と Run fixture で実 Registry を構築する。"""

    registry = _registry(provider)
    context = _context(tmp_path, registry)
    return registry.build_gateway_runtime(context, audit_writer=writer), context.tools[0].sdk_name


def _arguments(issue_ref: str = "TICKET-1") -> dict[str, Any]:
    """入れ子の list を含む合法 issue.read 引数を返す。"""

    return {"issue_ref": issue_ref, "purpose": "analysis", "fields": ["subject"]}


async def _authorize(
    runtime: RunToolRuntime,
    name: str,
    arguments: dict[str, Any],
    use_id: str,
) -> None:
    """実 PreToolUse callback を通し、validator を迂回しない。"""

    assert runtime.mcp.on_tool_authorized is not None
    await runtime.mcp.on_tool_authorized(
        name, arguments, use_id, "c1fe65d8-50d5-4ad8-b548-d436e31f68a0"
    )


def _payload(response: dict[str, Any]) -> dict[str, Any]:
    """実 MCP response から公開 JSON を取り出す。"""

    value = json.loads(response["content"][0]["text"])
    assert isinstance(value, dict)
    return value


async def _entered(event: asyncio.Event) -> None:
    """回帰不成立時も test task を無期限に残さない。"""

    await asyncio.wait_for(event.wait(), timeout=2)


@pytest.mark.asyncio
async def test_duplicate_pretool_running_does_not_execute_provider_again(tmp_path: Path) -> None:
    """同じ SDK ID の未決呼び出しは並行 Provider 実行を追加しない。"""

    writer = OwnershipAuditWriter()
    provider = PausingIssueProvider(pause_first=True)
    runtime, name = _runtime(tmp_path, provider, writer)
    arguments = _arguments()
    await _authorize(runtime, name, arguments, "same-use")
    original = asyncio.create_task(runtime.gateway.invoke_mcp(name, arguments))
    try:
        await _entered(provider.entered)
        await _authorize(runtime, name, arguments, "same-use")
        duplicate = await runtime.gateway.invoke_mcp(name, arguments)
        assert duplicate.get("is_error") is True
        assert _payload(duplicate)["code"] == "unavailable"
        assert provider.started == 1
        assert writer.failed == []
    finally:
        provider.release.set()
        result = await original
    assert not result.get("is_error")
    assert len(writer.completed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["RUNNING", "FAILED", "DENIED"])
async def test_existing_non_success_is_not_new_execution_authority(
    tmp_path: Path, status: str
) -> None:
    """保存済み未決・拒否・失敗の原呼び出しを再実行や fail で上書きしない。"""

    writer = OwnershipAuditWriter()
    provider = PausingIssueProvider()
    runtime, name = _runtime(tmp_path, provider, writer)
    saved = ToolAuditLease(uuid4(), status)
    writer.by_use_id["old-use"] = saved
    await _authorize(runtime, name, _arguments(), "old-use")
    result = await runtime.gateway.invoke_mcp(name, _arguments())
    assert result.get("is_error") is True
    assert _payload(result)["code"] == "unavailable"
    assert provider.started == 0
    assert writer.completed == []
    assert writer.failed == []
    assert writer.by_use_id["old-use"] is saved


@pytest.mark.asyncio
async def test_success_replays_exact_original_result_without_provider(tmp_path: Path) -> None:
    """成功の再読取は原 Evidence ref と nested response をそのまま返す。"""

    writer = OwnershipAuditWriter()
    provider = PausingIssueProvider()
    runtime, name = _runtime(tmp_path, provider, writer)
    await _authorize(runtime, name, _arguments(), "successful-use")
    first = await runtime.gateway.invoke_mcp(name, _arguments())
    await _authorize(runtime, name, _arguments(), "successful-use")
    replay = await runtime.gateway.invoke_mcp(name, _arguments())
    assert first == replay
    assert provider.started == 1
    assert len(writer.completed) == 1
    assert len(writer.dispatches) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("port_reference", ["start_authorized", "verify_dispatch"])
async def test_replay_result_is_frozen_before_dispatch_await(
    tmp_path: Path, port_reference: str
) -> None:
    """port が受け渡した応答参照を認可待機中に変更しても別結果を再生しない。"""

    writer = OwnershipAuditWriter()
    provider = PausingIssueProvider()
    runtime, name = _runtime(tmp_path, provider, writer)
    await _authorize(runtime, name, _arguments(), "successful-use")
    first = await runtime.gateway.invoke_mcp(name, _arguments())
    writer.pause = "dispatch"
    await _authorize(runtime, name, _arguments(), "successful-use")
    pending = asyncio.create_task(runtime.gateway.invoke_mcp(name, _arguments()))
    try:
        await _entered(writer.entered)
        # 私有 queue ではなく start の返値または verify 引数という公開 port 参照を使う。
        stored = (
            writer.by_use_id["successful-use"].result
            if port_reference == "start_authorized"
            else writer.dispatches[-1].result
        )
        assert stored is not None
        stored["issue"]["fields"]["labels"].append("changed")
    finally:
        writer.release.set()
        replay = await pending
    assert replay == first
    assert provider.started == 1
    assert len(writer.completed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [False, True])
async def test_dispatch_authority_failure_cannot_execute_or_return_saved_result(
    tmp_path: Path, replay: bool
) -> None:
    """原実行権の再確認失敗は新規実行も成功済み結果の公開も止める。"""

    class RefusedDispatchWriter(OwnershipAuditWriter):
        """実 DB 検査の拒否を Gateway 境界へ渡す観測 port。"""

        refuse = False

        async def verify_dispatch(self, lease: ToolAuditLease) -> None:
            """原 lease の拒否で ToolCall を fail に書き換えないことを試す。"""

            await super().verify_dispatch(lease)
            if self.refuse:
                raise PermissionError("Synthetic authority unavailable")

    writer = RefusedDispatchWriter()
    provider = PausingIssueProvider()
    runtime, name = _runtime(tmp_path, provider, writer)
    if replay:
        await _authorize(runtime, name, _arguments(), "same-use")
        first = await runtime.gateway.invoke_mcp(name, _arguments())
        assert not first.get("is_error")
    writer.refuse = True
    await _authorize(runtime, name, _arguments(), "same-use")
    denied = await runtime.gateway.invoke_mcp(name, _arguments())
    assert denied.get("is_error") is True
    assert _payload(denied)["code"] == "unavailable"
    assert provider.started == int(replay)
    assert len(writer.completed) == int(replay)
    assert writer.failed == []


@pytest.mark.asyncio
async def test_a_second_new_flag_cannot_reuse_consumed_tool_call(tmp_path: Path) -> None:
    """不良 port が新規と再主張しても同一 Gateway は原 ToolCall を再実行しない。"""

    class RepeatedNewWriter(OwnershipAuditWriter):
        """同一認領を二回とも新規と報告する意図的に不良な port。"""

        async def start_authorized(self, invocation: ToolInvocation) -> ToolAuditLease:
            """永続 port の防御とは独立した Gateway の一回性を試す。"""

            return replace(await super().start_authorized(invocation), is_new=True)

    writer = RepeatedNewWriter()
    provider = PausingIssueProvider(pause_first=True)
    runtime, name = _runtime(tmp_path, provider, writer)
    await _authorize(runtime, name, _arguments(), "same-use")
    original = asyncio.create_task(runtime.gateway.invoke_mcp(name, _arguments()))
    try:
        await _entered(provider.entered)
        await _authorize(runtime, name, _arguments(), "same-use")
        duplicate = await runtime.gateway.invoke_mcp(name, _arguments())
        assert duplicate.get("is_error") is True
        assert provider.started == 1
        assert writer.failed == []
    finally:
        provider.release.set()
        await original


@pytest.mark.asyncio
async def test_pretool_argument_snapshot_survives_audit_await(tmp_path: Path) -> None:
    """PreToolUse の fingerprint と監査引数は元 list の後続変更と分離される。"""

    writer = OwnershipAuditWriter(pause="start")
    provider = PausingIssueProvider()
    runtime, name = _runtime(tmp_path, provider, writer)
    arguments = _arguments()
    original = deepcopy(arguments)
    authorization = asyncio.create_task(_authorize(runtime, name, arguments, "frozen-use"))
    try:
        await _entered(writer.entered)
        arguments["fields"].append("description")
        arguments["issue_ref"] = "TICKET-CHANGED"
    finally:
        writer.release.set()
        await authorization
    result = await runtime.gateway.invoke_mcp(name, original)
    assert not result.get("is_error")
    assert provider.observed == [original]
    assert dict(next(iter(writer.invocations.values())).arguments) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["dispatch", "provider"])
async def test_handler_arguments_cannot_change_across_owned_await(
    tmp_path: Path, phase: str
) -> None:
    """handler の最初の await 前に固定した引数だけが Provider へ渡る。"""

    writer = OwnershipAuditWriter(pause="dispatch" if phase == "dispatch" else None)
    provider = PausingIssueProvider(pause_first=phase == "provider")
    runtime, name = _runtime(tmp_path, provider, writer)
    arguments = _arguments()
    original = deepcopy(arguments)
    await _authorize(runtime, name, arguments, "frozen-use")
    pending = asyncio.create_task(runtime.gateway.invoke_mcp(name, arguments))
    gate = writer if phase == "dispatch" else provider
    try:
        await _entered(gate.entered)
        arguments["fields"].append("description")
        arguments["issue_ref"] = "TICKET-CHANGED"
    finally:
        gate.release.set()
        result = await pending
    assert not result.get("is_error")
    assert _payload(result)["issue"]["id"] == "TICKET-1"
    assert provider.observed == [original]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["response", "source_locator", "metadata"])
async def test_validated_candidate_is_detached_before_complete_await(
    tmp_path: Path, target: str
) -> None:
    """応答検証後の Provider 元参照の変更は保存内容へ混ざらない。"""

    writer = OwnershipAuditWriter(pause="complete")
    provider = PausingIssueProvider()
    runtime, name = _runtime(tmp_path, provider, writer)
    await _authorize(runtime, name, _arguments(), "candidate-use")
    pending = asyncio.create_task(runtime.gateway.invoke_mcp(name, _arguments()))
    try:
        await _entered(writer.entered)
        assert provider.candidate is not None
        candidate = provider.candidate
        if target == "response":
            candidate.response["issue"]["fields"]["labels"].append("changed")
        elif target == "source_locator":
            candidate.evidence[0].source_locator["rows"].append(99)
        else:
            assert candidate.evidence[0].metadata is not None
            candidate.evidence[0].metadata["labels"].append("changed")
    finally:
        writer.release.set()
        result = await pending
    assert not result.get("is_error")
    assert _payload(result)["issue"]["fields"] == {"labels": ["original"]}
    saved_draft = writer.completed[0][1][0].draft
    assert saved_draft.source_locator == {"sheet": "issues", "rows": [2]}
    assert saved_draft.metadata == {"reproducibility": "snapshot", "labels": ["original"]}


@pytest.mark.asyncio
async def test_identical_arguments_keep_distinct_sdk_invocations_fifo(tmp_path: Path) -> None:
    """形が同じ二つの正規呼び出しを原 SDK ID 順に別 ToolCall として保存する。"""

    writer = OwnershipAuditWriter()
    provider = PausingIssueProvider()
    runtime, name = _runtime(tmp_path, provider, writer)
    for use_id in ("first-use", "second-use"):
        await _authorize(runtime, name, _arguments(), use_id)
    first = await runtime.gateway.invoke_mcp(name, _arguments())
    second = await runtime.gateway.invoke_mcp(name, _arguments())
    assert not first.get("is_error") and not second.get("is_error")
    assert _payload(first)["evidence_refs"] != _payload(second)["evidence_refs"]
    assert provider.started == 2
    assert [writer.invocations[item[0]].sdk_tool_use_id for item in writer.completed] == [
        "first-use",
        "second-use",
    ]


@pytest.mark.asyncio
async def test_reverse_handler_order_cannot_cross_argument_queues(tmp_path: Path) -> None:
    """別 fingerprint の handler 到着順が逆でも元引数の許可だけを消費する。"""

    writer = OwnershipAuditWriter()
    provider = PausingIssueProvider()
    runtime, name = _runtime(tmp_path, provider, writer)
    await _authorize(runtime, name, _arguments("TICKET-1"), "first-use")
    await _authorize(runtime, name, _arguments("TICKET-2"), "second-use")
    for issue_ref in ("TICKET-2", "TICKET-1"):
        result = await runtime.gateway.invoke_mcp(name, _arguments(issue_ref))
        assert _payload(result)["issue"]["id"] == issue_ref
    assert [writer.invocations[item[0]].sdk_tool_use_id for item in writer.completed] == [
        "second-use",
        "first-use",
    ]


@pytest.mark.asyncio
async def test_same_arguments_cannot_borrow_another_runtime_queue(tmp_path: Path) -> None:
    """別 Run runtime の同形入力は許可済み呼び出しを横取りできない。"""

    writer = OwnershipAuditWriter()
    provider = PausingIssueProvider()
    first, name = _runtime(tmp_path, provider, writer)
    other, other_name = _runtime(tmp_path, provider, OwnershipAuditWriter())
    await _authorize(first, name, _arguments(), "owned-use")
    denied = await other.gateway.invoke_mcp(other_name, _arguments())
    assert denied.get("is_error") is True
    assert provider.started == 0
    allowed = await first.gateway.invoke_mcp(name, _arguments())
    assert not allowed.get("is_error")
    assert provider.started == 1


@pytest.mark.asyncio
async def test_commit_response_loss_never_overwrites_or_reexecutes_original(tmp_path: Path) -> None:
    """完了 commit の応答喪失を fail で書き換えず、原成功の再読取だけを行う。"""

    writer = OwnershipAuditWriter()
    writer.commit_response_lost = True
    provider = PausingIssueProvider()
    runtime, name = _runtime(tmp_path, provider, writer)
    await _authorize(runtime, name, _arguments(), "unknown-use")
    lost = await runtime.gateway.invoke_mcp(name, _arguments())
    assert lost.get("is_error") is True
    assert writer.failed == []
    await _authorize(runtime, name, _arguments(), "unknown-use")
    confirmed = await runtime.gateway.invoke_mcp(name, _arguments())
    assert not confirmed.get("is_error")
    assert _payload(confirmed) == writer.saved[0]
    assert provider.started == 1
    assert len(writer.completed) == 1


@pytest.mark.asyncio
async def test_cancelled_provider_does_not_grant_retry_execution(tmp_path: Path) -> None:
    """ローカル task の取消は永続 RUNNING を新たな実行権へ変換しない。"""

    writer = OwnershipAuditWriter()
    provider = PausingIssueProvider(pause_first=True)
    runtime, name = _runtime(tmp_path, provider, writer)
    await _authorize(runtime, name, _arguments(), "cancelled-use")
    pending = asyncio.create_task(runtime.gateway.invoke_mcp(name, _arguments()))
    try:
        await _entered(provider.entered)
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    await _authorize(runtime, name, _arguments(), "cancelled-use")
    retry = await runtime.gateway.invoke_mcp(name, _arguments())
    assert retry.get("is_error") is True
    assert provider.started == 1
    assert writer.completed == []
    assert writer.failed == []
