"""同一 Segment の技術再試行で、原指示と増分事実・回执を分離する。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.agent.context_builder import ProductionRunContextBuilder
from skillmind.agent.continuation_prompt import continuation_prompt
from skillmind.agent.contract_store import ContractStore
from skillmind.agent.task_brief import render_task_brief_prompt
from skillmind.agent.tool_catalog import create_run_tool_registry
from skillmind.agent.workspace import WorkspaceManager
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import AgentTaskBriefSnapshot
from skillmind.runs.domain import ConcurrentRunUpdateError
from skillmind.runs.repository import RunRepository
from tests.agent.test_effect_receipt_delivery import large_receipt
from tests.agent.test_runtime_context import (
    CONTRACTS,
    _generic_claimed,
    _generic_manifest,
    _NoopDocumentSource,
)
from tests.runs.test_execution_gates import execution_rows, row_result


def retry_case(tmp_path, *, source_execution=False):
    """実 Builder に、確定済み原回执の読取境界だけを代入する。"""
    manifest = _generic_manifest(required=False)
    manifest["tools"] = [{"capability": "workspace.read/v1", "required": True}]
    manifest["capability_blueprint"]["execution_preferences"] = {"recommended_profile": "GUIDED"}
    claimed = replace(_generic_claimed(
        manifest=manifest, selected_sources={}, allowed=("workspace.read/v1",),
    ), run_segment_id=uuid4())
    claimed.permission_snapshot_json["execution_profile"] = "GUIDED"
    claimed.task_snapshot_json["runtime_policy"] = "skillmind.runtime/v8"
    if source_execution:
        from tests.skills.test_source_execution import compile_case

        _, manifest = compile_case()
        checksum = "sha256:" + sha256_hex(canonical_json(manifest))
        claimed = replace(claimed, input_json={}, task_snapshot_json={
            **claimed.task_snapshot_json, "task_key": "execute",
            "capability": manifest["tasks"][0]["capability"], "manifest_checksum": checksum,
            "input_schema_json": manifest["tasks"][0]["input_schema"],
        }, skill_snapshots_json=({**claimed.skill_snapshots_json[0],
            "manifest": manifest, "manifest_checksum": checksum},))
        claimed.permission_snapshot_json.update(execution_profile="SUPERVISED",
            resource_operations=[])
    originals = []

    async def saved_receipts(_claimed):
        """外部操作を実行せず、同一 Run で確定した原回执だけを提供する。"""
        for original in originals:
            yield original

    reader = SimpleNamespace(receipts=AsyncMock(return_value=[]),
        load=AsyncMock(return_value=None), materialization_receipts=saved_receipts)
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager(tmp_path / "runs"),
        tool_registry=create_run_tool_registry(ContractStore(CONTRACTS),
            document_source=_NoopDocumentSource()),
        model="synthetic-model", proposal_continuations=reader,
    )
    return builder, claimed, originals


@pytest.mark.parametrize("segment_no", [1, 2])
@pytest.mark.parametrize("source_execution", [False, True])
async def test_retry_reuses_frozen_brief_and_delivers_latest_checkpoint(
    tmp_path, segment_no, source_execution,
):
    """即時書込後も原 Brief/checksum を保ち、全文・差分続行に新事実と原回执を渡す。"""
    builder, claimed, originals = retry_case(tmp_path, source_execution=source_execution)
    claimed = replace(claimed, segment_no=segment_no,
        checkpoint_json={"summary": "Ready", "confirmed_facts": ["original fact"]})
    first = await builder.build(claimed, sequence_start=1)
    original_json = canonical_json(first.task_brief)
    _, previous = continuation_prompt(first, first.prompt, None)
    original = large_receipt()
    originals.append(original)
    latest = {"summary": "Saved", "confirmed_facts": ["original fact", "new fact"],
        "evidence_refs": [original["after_ref"]],
        "change_proposal_refs": [original["proposal_ref"]]}
    retry = replace(claimed, run_attempt_id=uuid4(), attempt_no=2,
        checkpoint_json=latest, frozen_brief_json=deepcopy(first.task_brief),
        frozen_brief_checksum=first.task_brief_checksum)
    context = await builder.build(retry, sequence_start=20)
    assert context.task_brief == first.task_brief
    assert context.task_brief_checksum == first.task_brief_checksum
    assert canonical_json(first.task_brief) == original_json
    assert context.continuation_checkpoint["confirmed_facts"] == latest["confirmed_facts"]
    assert context.continuation_checkpoint["effect_receipts"] == [original]
    assert context.continuation_checkpoint["change_proposal_refs"] == latest["change_proposal_refs"]
    file = context.effect_receipt_files[0]["file"]
    assert json.loads((context.workspace.root / file["path"]).read_bytes()) == original
    assert "new fact" in context.prompt and file["path"] in context.prompt
    assert "x" * 100 not in context.prompt
    delta, updated = continuation_prompt(context, context.prompt, previous)
    assert '"new fact"' in delta and '"original fact"' not in delta
    assert file["path"] in delta and original["effect_execution_id"] in delta
    repeated, _ = continuation_prompt(context, context.prompt, updated)
    assert '"new fact"' not in repeated and file["path"] not in repeated
    # 次の業務 Segment は最新事実を自身の新 Brief として凍結する。
    next_segment = replace(retry, run_segment_id=uuid4(), segment_no=segment_no + 1,
        attempt_no=1, frozen_brief_json=None, frozen_brief_checksum="")
    following = await builder.build(next_segment, sequence_start=30)
    assert following.continuation_checkpoint is None
    assert following.task_brief["checkpoint"]["confirmed_facts"] == latest["confirmed_facts"]
    assert following.task_brief_checksum != first.task_brief_checksum


@pytest.mark.parametrize("invalid", [
    "checksum", "run", "segment", "instructions", "limits", "tools", "model",
])
async def test_retry_still_rejects_changed_frozen_instructions(tmp_path, invalid):
    """新 checkpoint を受け入れても、指示・実行帰属・制限の差替えは許可しない。"""
    builder, claimed, _ = retry_case(tmp_path)
    first = await builder.build(claimed, sequence_start=1)
    frozen = deepcopy(first.task_brief)
    checksum = first.task_brief_checksum
    if invalid == "checksum":
        checksum = "sha256:" + "0" * 64
    elif invalid == "run":
        frozen["identity"]["run_id"] = str(uuid4())
    elif invalid == "segment":
        frozen["identity"]["segment_no"] += 1
    elif invalid == "instructions":
        frozen["objective"]["run_objective"] = "Changed task"
    elif invalid == "tools":
        frozen["allowed_tools"] = []
    elif invalid == "model":
        frozen["runtime_metadata"]["model"] = "changed-model"
    else:
        claimed.limits_snapshot_json["max_turns"] += 1
    if invalid != "checksum":
        checksum = "sha256:" + sha256_hex(canonical_json(frozen))
    with pytest.raises(ValueError, match="Frozen AgentTaskBrief"):
        await builder.build(replace(claimed, attempt_no=2,
            frozen_brief_json=frozen, frozen_brief_checksum=checksum), sequence_start=2)


@pytest.mark.parametrize("file_failure", [False, True])
async def test_retry_without_receipt_files_keeps_original_receipts_inline(
    tmp_path, monkeypatch, file_failure,
):
    """file 交付の失敗でも新回执を失わず、原 Brief は変更しない。"""
    builder, claimed, originals = retry_case(tmp_path)
    first = await builder.build(claimed, sequence_start=1)
    originals.append(large_receipt())
    if file_failure:
        from skillmind.agent.materialization_storage import MaterializationError

        def unavailable(*args, **kwargs):
            """原回执を保持したまま、任意の file 副本だけを失敗させる。"""
            raise MaterializationError("synthetic failure")

        monkeypatch.setattr("skillmind.agent.effect_receipt_delivery.write_workspace_file",
            unavailable)
    else:
        originals[0]["after"] = {"rows": []}
        originals[0]["after_content_hash"] = "sha256:" + sha256_hex(canonical_json({"rows": []}))
    context = await builder.build(replace(claimed, attempt_no=2,
        frozen_brief_json=first.task_brief, frozen_brief_checksum=first.task_brief_checksum),
        sequence_start=2)
    assert not context.effect_receipt_files
    assert canonical_json(originals[0]) in context.prompt
    assert context.task_brief_checksum == first.task_brief_checksum


@pytest.mark.parametrize("change", ["none", "checksum", "checkpoint"])
async def test_existing_freeze_guard_remains_active(change):
    """再試行も原 lock/lease 付き freeze を通り、正本の書換えは拒否する。"""
    claimed, run, segment, attempt = execution_rows()
    brief = {"brief_version": "skillmind.agent-task-brief/v2",
        "identity": {"run_id": str(run.id), "segment_no": segment.segment_no},
        "checkpoint": {"summary": None}}
    checksum = "sha256:" + sha256_hex(canonical_json(brief))
    frozen = AgentTaskBriefSnapshot(id=uuid4(), run_id=run.id, run_segment_id=segment.id,
        brief_json=deepcopy(brief), checksum=checksum)
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(side_effect=[row_result(row) for row in
        (run, segment, attempt, frozen)])
    session.scalar = AsyncMock(return_value=None)
    if change == "checksum":
        checksum = "sha256:" + "0" * 64
    elif change == "checkpoint":
        brief["checkpoint"]["summary"] = "Changed frozen checkpoint"
        checksum = "sha256:" + sha256_hex(canonical_json(brief))
    call = RunRepository(session).freeze_agent_task_brief(claimed, brief=brief, checksum=checksum)
    if change == "checksum":
        with pytest.raises(ValueError, match="checksum"):
            await call
    elif change == "checkpoint":
        with pytest.raises(ConcurrentRunUpdateError, match="changed after freezing"):
            await call
    else:
        await call
    session.add.assert_not_called()


def test_source_execution_full_and_delta_resume_use_separate_checkpoint(tmp_path):
    """現行原文方式も凍結 JSON を変更せず、cold/warm 続行の両方に新事実を渡す。"""
    from pathlib import Path

    from tests.agent.test_continuation_prompt import compiled, context_with_brief

    example = Path(__file__).resolve().parents[3] / "contracts/examples/agent-task-brief.v2.json"
    first = compiled(context_with_brief(tmp_path), json.loads(example.read_text()))
    _, previous = continuation_prompt(first, first.prompt, None)
    checkpoint = {**first.task_brief["checkpoint"], "confirmed_facts": ["new exact fact"]}
    current = replace(first, continuation_checkpoint=checkpoint,
        prompt=render_task_brief_prompt(first.task_brief, input_json=first.input_json,
            output_schema=first.result_schema, continuation_checkpoint=checkpoint))
    assert "new exact fact" in current.prompt
    delta, _ = continuation_prompt(current, current.prompt, previous)
    assert "new exact fact" in delta
    assert "Frozen Skill sources" not in delta
    assert current.task_brief == first.task_brief
