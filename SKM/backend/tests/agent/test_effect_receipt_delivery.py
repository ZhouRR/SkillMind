"""モデル交付を小さくしても、原回执・回読・復旧と新たな書込の境界を保つ。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.agent.codex_mcp import CodexToolBridge
from skillmind.agent.context_builder import ProductionRunContextBuilder
from skillmind.agent.continuation_prompt import continuation_prompt
from skillmind.agent.contract_store import ContractStore
from skillmind.agent.effect_receipt_delivery import (
    deliver_receipt,
    materialize_receipt_files,
    model_checkpoint,
    project_receipt,
)
from skillmind.agent.materialization_storage import MaterializationError
from skillmind.agent.task_brief import render_task_brief_prompt
from skillmind.agent.tool_catalog import (
    _change_propose_tool_definition,
    _workspace_tool_definitions,
    create_run_tool_registry,
)
from skillmind.agent.tool_gateway import ToolRegistry
from skillmind.agent.workspace import WorkspaceManager
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.inline import InlineEffectResult
from tests.agent.test_continuation_prompt import context_with_brief
from tests.agent.test_runtime_context import (
    _generic_claimed,
    _generic_manifest,
    _NoopDocumentSource,
)
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _context, _registry
from tests.agent.test_tool_policy import _database_proposal
from tests.agent.test_workspace_provider import CONTRACTS
from tests.runs.test_effect_continuation import receipt


def large_receipt():
    """業務判定を含まない合成の原回读を約 48 KiB にし、hash を正確に再計算する。"""
    value = receipt()
    value["after"] = {"rows": [{"status": "ERROR", "result_ref": "x" * 48_000}]}
    value["after_content_hash"] = "sha256:" + sha256_hex(canonical_json(value["after"]))
    return value


def run_context(tmp_path):
    """実 Run directory と、実契約の workspace 読取 Tool を組み立てる。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    context = _context(tmp_path, _registry(CsvIssueProvider()))
    registry = ToolRegistry(_workspace_tool_definitions(ContractStore(CONTRACTS)))
    return replace(context,
        workspace=WorkspaceManager(tmp_path / "runs").initialize(context.run_id),
        tools=(registry.resolve_unbound("workspace.read/v1", execution_profile="GUIDED"),),
        permission_snapshot={"allowed_capabilities": ["workspace.read/v1"]},
    )


async def test_large_receipt_is_exact_and_model_reply_is_small(tmp_path):
    """モデルへ業務 ERROR を隠れた PASS として返さず、原 JSON を file から取得できる。"""
    context = run_context(tmp_path)
    original = large_receipt()
    before = deepcopy(original)
    projected = await deliver_receipt(context, original)
    file = projected["file"]
    raw = (context.workspace.root / file["path"]).read_bytes()
    assert json.loads(raw) == original == before
    assert file["content_hash"] == "sha256:" + sha256_hex(raw)
    assert file["size_bytes"] == len(raw)
    assert projected["status"] == "APPLIED"
    assert projected["after_ref"] == original["after_ref"]
    assert projected["verification"] == original["verification"]
    assert projected["after_keys"] == ["rows"]
    assert "after" not in projected and len(canonical_json(projected).encode()) < 1_500
    assert "PASS" not in canonical_json(projected)


async def test_small_receipt_uses_no_extra_file_or_read(tmp_path):
    """普通の短い原回执は既存の一応答で返し、機械的な追加読取を作らない。"""
    context = run_context(tmp_path)
    assert await deliver_receipt(context, receipt()) == receipt()
    assert not (context.workspace.cwd / "resources").exists()


@pytest.mark.parametrize("missing", ["permission", "tool"])
async def test_delivery_does_not_add_workspace_access(tmp_path, missing):
    """原権限または公開 Tool がない Run へ file 読取を後付けしない。"""
    context = run_context(tmp_path)
    context = replace(context, permission_snapshot={"allowed_capabilities": []}) if (
        missing == "permission"
    ) else replace(context, tools=())
    assert await deliver_receipt(context, large_receipt()) == large_receipt()
    assert not (context.workspace.cwd / "resources").exists()


async def test_file_failure_keeps_the_confirmed_original_receipt(tmp_path, monkeypatch):
    """保存済み業務は file 交付の失敗で止めず、完全な原本文へ戻す。"""
    context = run_context(tmp_path)

    def unavailable(*args, **kwargs):
        """追加の file だけを書けない環境を再現する。"""
        raise MaterializationError("synthetic unavailable")

    monkeypatch.setattr("skillmind.agent.effect_receipt_delivery.write_workspace_file", unavailable)
    assert await deliver_receipt(context, large_receipt()) == large_receipt()


async def test_missing_local_copy_is_restored_from_original_receipt(tmp_path):
    """Worker 再準備では遠端再観測せず、保持した原回执から同一 byte を復元する。"""
    context = run_context(tmp_path)
    original = large_receipt()
    first = await deliver_receipt(context, original)
    path = context.workspace.root / first["file"]["path"]
    path.unlink()
    assert await deliver_receipt(context, original) == first
    assert path.read_bytes() == canonical_json(original).encode()
    path.write_bytes(b"modified")
    assert await deliver_receipt(context, original) == original
    assert path.read_bytes() == b"modified"


async def test_resume_projects_receipt_once_and_keeps_frozen_audit_unchanged(tmp_path):
    """全量の監査 checksum を保ち、同じ回执を latest と累積一覧へ二重交付しない。"""
    old = context_with_brief(tmp_path)
    old = replace(old, workspace=WorkspaceManager(tmp_path / "prepared").initialize(old.run_id))
    _, previous = continuation_prompt(old, old.prompt, None)
    original = large_receipt()
    files = await materialize_receipt_files(
        old.workspace, (original,), ("workspace.read/v1",),
    )
    brief = deepcopy(old.task_brief)
    brief["identity"]["segment_no"] = 2
    brief["checkpoint"].update(effect_result=original, effect_receipts=[original])
    checksum = "sha256:" + sha256_hex(canonical_json(brief))
    context = replace(old, task_brief=brief, task_brief_checksum=checksum,
        effect_receipt_files=files,
        prompt=render_task_brief_prompt(brief, input_json=old.input_json,
            output_schema=old.result_schema, effect_receipt_files=files))
    prompt, _ = continuation_prompt(context, context.prompt, previous)
    assert "x" * 100 not in prompt and files[0]["file"]["path"] in prompt
    assert model_checkpoint(brief, files)["effect_receipts"] == []
    assert brief["checkpoint"]["effect_result"] == original
    assert context.task_brief_checksum == "sha256:" + sha256_hex(canonical_json(brief))
    assert len(prompt.encode()) < 4_000


async def test_production_context_restores_files_and_preserves_audit_on_delivery_failure(
    tmp_path, monkeypatch,
):
    """実組成でも file は新しい業務依存にならず、同じ凍結 Brief から復元/全文交付できる。"""
    original = large_receipt()
    manifest = _generic_manifest(required=False)
    manifest["tools"] = [{"capability": "workspace.read/v1", "required": True}]
    manifest["capability_blueprint"]["execution_preferences"] = {"recommended_profile": "GUIDED"}
    claimed = _generic_claimed(
        selected_sources={}, manifest=manifest, allowed=("workspace.read/v1",),
    )
    claimed.permission_snapshot_json["execution_profile"] = "GUIDED"
    claimed.task_snapshot_json["runtime_policy"] = "skillmind.runtime/v8"
    claimed = replace(claimed, segment_no=2, checkpoint_json={"effect_result": original})
    inline = large_receipt()
    inline["effect_execution_id"] = str(uuid4())

    async def saved_receipts(_claimed):
        """checkpoint にない即時回执も同じ Run の正本として提供する。"""
        yield inline

    reader = SimpleNamespace(receipts=AsyncMock(return_value=[original]),
        load=AsyncMock(return_value=None), materialization_receipts=saved_receipts)
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager(tmp_path / "runs"),
        tool_registry=create_run_tool_registry(ContractStore(CONTRACTS),
            document_source=_NoopDocumentSource()),
        model="synthetic-model", proposal_continuations=reader,
    )
    first = await builder.build(claimed, sequence_start=1)
    assert "x" * 100 not in first.prompt
    assert first.task_brief["checkpoint"]["effect_result"] == original
    file = first.effect_receipt_files[0]["file"]
    path = first.workspace.root / file["path"]
    cached_inline = await deliver_receipt(first, inline)
    inline_path = first.workspace.root / cached_inline["file"]["path"]
    path.unlink()
    inline_path.unlink()
    restored = await builder.build(claimed, sequence_start=2)
    assert path.exists() and inline_path.exists()
    assert restored.task_brief_checksum == first.task_brief_checksum

    def unavailable(*args, **kwargs):
        """物化の局所的な failure だけを追加し、正本は変更しない。"""
        raise MaterializationError("synthetic unavailable")

    monkeypatch.setattr("skillmind.agent.effect_receipt_delivery.write_workspace_file", unavailable)
    fallback = await builder.build(claimed, sequence_start=3)
    assert not fallback.effect_receipt_files and "x" * 100 in fallback.prompt
    assert fallback.task_brief_checksum == first.task_brief_checksum


async def test_native_bridge_returns_large_applied_receipt_without_parking(tmp_path):
    """共有 Effect の確定後だけ file 参照を返し、SDK turn と元操作 ID を保持する。"""
    contracts = ContractStore(CONTRACTS)
    registry = ToolRegistry((
        _change_propose_tool_definition(contracts), *_workspace_tool_definitions(contracts),
    ))
    context = run_context(tmp_path)
    tool = registry.resolve_unbound("change.propose/v1", execution_profile="GUIDED")
    context = replace(context, tools=(tool, *context.tools),
        permission_snapshot={"allowed_capabilities": [tool.capability, "workspace.read/v1"]})
    runtime = registry.build_gateway_runtime(context, audit_writer=MemoryAuditWriter())
    original = large_receipt()
    effect = AsyncMock(return_value=InlineEffectResult(uuid4(), original))
    runtime = replace(runtime, mcp=replace(runtime.mcp, on_inline_effect=effect))
    bridge = CodexToolBridge(context, runtime, on_deferred=AsyncMock())
    bridge.session_id = str(uuid4())
    arguments, *_ = _database_proposal()
    result = await bridge.invoke(tool.sdk_name, arguments, "original-call")
    assert not result.isError and bridge.accepting and not bridge.parked.is_set()
    data = json.loads(result.content[0].text)
    assert data["outcome"] == "APPLIED" and data["delivery"] == "INLINE"
    assert data["effect_result"]["file"]["size_bytes"] > 48_000
    effect.assert_awaited_once_with(arguments, "original-call", bridge.session_id)


async def test_large_verification_and_foreign_file_are_not_implicit_success(tmp_path):
    """任意 Provider の大きい検証値も残し、別回执の file を結び付けない。"""
    context = run_context(tmp_path)
    original = large_receipt()
    original["verification"] = {"checks": "v" * 10_000}
    files = await materialize_receipt_files(context.workspace, (original,), ("workspace.read/v1",))
    projected = project_receipt(original, files)
    assert "verification" not in projected and projected["verification_pointer"] == "/verification"
    changed = deepcopy(files)
    changed[0]["file"]["content_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="original bytes"):
        project_receipt(original, changed)
