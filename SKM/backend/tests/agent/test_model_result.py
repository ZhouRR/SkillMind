"""モデルの短参照から原保存包絡へ戻し、所有・原回執・内容検査を維持する。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from skillmind.agent.claude import ClaudeRuntimeConfiguration, build_claude_agent_options
from skillmind.agent.codex_schema import CodexOutputSchema
from skillmind.agent.model_result import (
    PostgresResultAssemblyLookup,
    ResultAssemblyError,
    ResultAssemblySnapshot,
    assemble_model_result,
    compile_model_result_schema,
    project_result_references,
    reference_label,
)
from skillmind.agent.outcome import compile_outcome_schema
from skillmind.agent.result_validation import ResultValidationError, ResultValidator
from skillmind.artifacts.domain import ArtifactIntegrityError
from tests.agent.test_result_references import ProposalLookup
from tests.agent.test_result_validation import MemoryEvidenceLookup
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _context, _registry

EVIDENCE = "ev_" + "1" * 32
OTHER = "ev_" + "2" * 32
ARTIFACT = "art_" + "3" * 32


def draft():
    """監査 field や長い ID を書かず、業務の結論と実際の短参照を選ぶ。"""
    return {
        "summary": "レビューを完了し、確認事項を記録しました。", "status": "COMPLETED",
        "deliverables": [{"key": "report", "kind": "report", "title": "レビュー報告",
                          "artifact": reference_label(ARTIFACT)}],
        "findings": [{"key": "finding", "title": "確認事項", "detail": "前提条件が不足する。",
                      "sources": [reference_label(EVIDENCE)]}],
        "open_questions": [], "limitations": [], "confidence": 0.9, "needs_review": True,
    }


def model_context(tmp_path):
    """保存契約と、同じ新規 Run に固定されたモデル契約を分離する。"""
    original = _context(tmp_path, _registry(CsvIssueProvider()))
    stored = compile_outcome_schema(None, task_schema_checksum=None)
    return replace(original, result_schema=stored.schema, task_snapshot={
        "result_kind": "OUTCOME_ENVELOPE", "output_schema_checksum": stored.checksum,
        "model_output_schema_json": compile_model_result_schema(stored.schema),
    })


def validator(context, snapshot):
    """組立は保存済み事実の fake、最終判定は本物の共通 ResultValidator を使う。"""
    assembly = AsyncMock()
    assembly.load.return_value = snapshot
    artifacts = AsyncMock()
    artifacts.verified_refs.return_value = frozenset({ARTIFACT})
    effects = AsyncMock()
    effects.invalid_refs.return_value = frozenset()
    return ResultValidator(
        MemoryEvidenceLookup(frozenset(snapshot.evidence_refs)), ProposalLookup(context.run_id),
        artifact_lookup=artifacts, effect_lookup=effects, assembly_lookup=assembly,
    ), assembly, artifacts, effects


def test_model_contract_is_compact_native_and_storage_contract_is_unchanged(tmp_path):
    """SDK には業務項目だけを渡し、保存包絡と二重 JSON 化の退行を検出する。"""
    context = model_context(tmp_path)
    schema = context.task_snapshot["model_output_schema_json"]
    Draft202012Validator(schema).validate(draft())
    assert set(schema["properties"]) == {
        "summary", "status", "deliverables", "findings", "open_questions", "limitations",
        "confidence", "needs_review",
    }
    assert "result_json" not in CodexOutputSchema(schema).schema["properties"]
    assert "effects" in context.result_schema["properties"]
    options = build_claude_agent_options(
        context, mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
        configuration=ClaudeRuntimeConfiguration(environment={}),
    )
    assert options.output_format["schema"] == schema


async def test_assembles_exact_selected_references_and_saved_effects(tmp_path):
    """無関係な観測は根拠へ追加せず、操作状態と ID はモデルから受け取らない。"""
    context = model_context(tmp_path)
    snapshot = ResultAssemblySnapshot(
        evidence_refs=(EVIDENCE, OTHER), artifact_refs=(ARTIFACT,), proposal_refs=("cp_saved",),
        effects=({"proposal_ref": "cp_saved", "status": "FAILED", "summary": "保存済み失敗",
                  "before_ref": EVIDENCE},),
    )
    value = draft()
    original = deepcopy(value)
    service, assembly, _, effects = validator(context, snapshot)
    result = await service.validate_context(context, structured_output=value)
    assert value == original
    assert result.data["findings"][0]["evidence_refs"] == [EVIDENCE]
    assert "sources" not in result.data["findings"][0]
    assert result.data["deliverables"][0]["artifact_ref"] == ARTIFACT
    assert result.data["effects"] == list(snapshot.effects)
    assert result.data["change_proposal_refs"] == ["cp_saved"]
    assert result.evidence_refs == {EVIDENCE} and OTHER not in result.evidence_refs
    assert result.artifact_refs == {ARTIFACT}
    assembly.load.assert_awaited_once_with(context, None)
    assert effects.invalid_refs.await_args.args[1][0].status == "FAILED"


@pytest.mark.parametrize("damage", ["unknown_evidence", "unknown_artifact", "wrong_kind"])
async def test_short_references_require_owned_exact_matches(tmp_path, damage):
    """存在しない/別 Run の短参照を削除や似た ID で救済しない。"""
    context = model_context(tmp_path)
    snapshot = ResultAssemblySnapshot(evidence_refs=(EVIDENCE,), artifact_refs=(ARTIFACT,))
    value = draft()
    if damage == "unknown_evidence":
        value["findings"][0]["sources"] = [reference_label(OTHER)]
    elif damage == "unknown_artifact":
        value["deliverables"][0]["artifact"] = "A" + "f" * 12
    else:
        value["findings"][0]["sources"] = [reference_label(ARTIFACT)]
    service, *_ = validator(context, snapshot)
    with pytest.raises(ResultValidationError) as caught:
        await service.validate_context(context, structured_output=value)
    assert caught.value.code in {
        "evidence_reference_invalid", "artifact_reference_invalid", "result_schema_invalid",
    }


def test_short_reference_collision_is_rejected(monkeypatch):
    """短縮値が衝突しても、一つ目や最後の証拠へ勝手に結び直さない。"""
    monkeypatch.setattr("skillmind.agent.model_result.reference_label", lambda _: "Ecollision")
    with pytest.raises(ResultAssemblyError, match="ambiguous"):
        assemble_model_result(draft(), ResultAssemblySnapshot(evidence_refs=(EVIDENCE, OTHER)))


@pytest.mark.parametrize("field,value", [
    ("evidence_refs", [EVIDENCE]), ("effects", []), ("artifact_refs", [ARTIFACT]),
    ("change_proposal_refs", ["cp_forged"]), ("outcome_version", "forged"),
])
async def test_model_cannot_inject_platform_audit_fields(tmp_path, field, value):
    """旧形状や勝手な監査申告を黙って無視せず、凍結モデル契約で拒否する。"""
    context = model_context(tmp_path)
    service, assembly, *_ = validator(context, ResultAssemblySnapshot())
    with pytest.raises(ResultValidationError, match="Schema"):
        await service.validate_context(context, structured_output={**draft(), field: value})
    assembly.load.assert_not_awaited()


async def test_pending_saved_proposal_cannot_be_omitted_by_model(tmp_path):
    """モデルが操作一覧を書かなくても、全原提案に未完了検査を適用する。"""
    context = model_context(tmp_path)
    snapshot = ResultAssemblySnapshot(evidence_refs=(EVIDENCE,), artifact_refs=(ARTIFACT,),
                                      proposal_refs=("cp_pending",))
    assembly = AsyncMock()
    assembly.load.return_value = snapshot
    artifacts = AsyncMock()
    artifacts.verified_refs.return_value = {ARTIFACT}
    service = ResultValidator(
        MemoryEvidenceLookup(frozenset({EVIDENCE})),
        ProposalLookup(context.run_id, incomplete=True),
        artifact_lookup=artifacts, assembly_lookup=assembly,
    )
    with pytest.raises(ResultValidationError) as caught:
        await service.validate_context(context, structured_output=draft())
    assert caught.value.code == "change_proposal_incomplete"


async def test_saved_artifact_bytes_are_still_verified(tmp_path):
    """短参照の解決だけで損傷した Artifact を有効扱いしない。"""
    context = model_context(tmp_path)
    service, _, artifacts, _ = validator(
        context, ResultAssemblySnapshot(evidence_refs=(EVIDENCE,), artifact_refs=(ARTIFACT,)),
    )
    artifacts.verified_refs.side_effect = ArtifactIntegrityError("invalid snapshot")
    with pytest.raises(ResultValidationError) as caught:
        await service.validate_context(context, structured_output=draft())
    assert caught.value.code == "artifact_reference_invalid"


async def test_business_schema_and_its_explicit_source_references_are_preserved(tmp_path):
    """任意業務 JSON を短参照用に書き換えず、宣言された根拠を全体索引へ統合する。"""
    context = model_context(tmp_path)
    task_schema = {"type": "object", "additionalProperties": False,
                   "required": ["source_ref"], "properties": {"source_ref": {"type": "string"}}}
    checksum = "sha256:" + "a" * 64
    stored = compile_outcome_schema(task_schema, task_schema_checksum=checksum)
    context = replace(context, result_schema=stored.schema, task_snapshot={
        **context.task_snapshot, "output_schema_checksum": stored.checksum,
        "task_output_schema_json": task_schema, "task_output_schema_checksum": checksum,
        "model_output_schema_json": compile_model_result_schema(stored.schema),
    })
    service, *_ = validator(context, ResultAssemblySnapshot(
        evidence_refs=(EVIDENCE, OTHER), artifact_refs=(ARTIFACT,),
    ))
    value = {**draft(), "structured_data": {"source_ref": OTHER}}
    result = await service.validate_context(context, structured_output=value)
    assert result.data["structured_data"] == {"source_ref": OTHER}
    assert result.data["evidence_refs"] == sorted([EVIDENCE, OTHER])
    assert result.validation["task_schema_valid"] is True


async def test_model_candidate_is_frozen_before_audit_lookup_await(tmp_path):
    """観測の await 中に呼出元が候補を変更しても、検証対象を差し替えさせない。"""
    context = model_context(tmp_path)
    service, assembly, *_ = validator(
        context, ResultAssemblySnapshot(evidence_refs=(EVIDENCE,), artifact_refs=(ARTIFACT,)),
    )
    value = draft()
    original = deepcopy(value)

    async def load(*args):
        """外側の payload を変更し、内側が原候補を保持することを確認する。"""
        value["summary"] = "変更後の主張"
        return ResultAssemblySnapshot(evidence_refs=(EVIDENCE,), artifact_refs=(ARTIFACT,))

    assembly.load.side_effect = load
    result = await service.validate_context(context, structured_output=value)
    assert result.data["summary"] == original["summary"]


async def test_executor_persists_standard_envelope_from_model_business_result(tmp_path):
    """実 Worker 終端処理は短参照を保存せず、従来の結果と引用を成功時に保存する。"""
    from skillmind.agent.domain import AgentEventType
    from skillmind.runs.domain import RunStatus
    from skillmind.worker.executor import AgentRunExecutor
    from tests.worker.test_agent_run_executor import SequenceEngine, _claimed, _event, _service

    claimed = _claimed()
    original = model_context(tmp_path)
    context = replace(original, run_id=claimed.run_id, run_attempt_id=claimed.run_attempt_id,
                      project_id=claimed.project_id, user_id=claimed.actor_id, sequence_start=10)
    service, *_ = validator(
        context, ResultAssemblySnapshot(evidence_refs=(EVIDENCE,), artifact_refs=(ARTIFACT,)),
    )
    builder = MagicMock()
    builder.build = AsyncMock(return_value=context)
    session_id = str(uuid4())
    engine = SequenceEngine([
        _event(claimed, 10, AgentEventType.SESSION_STARTED, session_id=session_id),
        _event(claimed, 11, AgentEventType.RESULT_COMPLETED, session_id=session_id,
               payload={"structured_output": draft()}),
    ])
    run_service = _service()
    await AgentRunExecutor(
        run_service=run_service, context_builder=builder, engine=engine,
        result_validator=service, lease_seconds=60,
    ).execute(claimed)
    terminal = run_service.finalize_execution.await_args.kwargs
    assert terminal["target"] is RunStatus.SUCCEEDED
    saved = terminal["result"]
    assert saved.result_kind == "OUTCOME_ENVELOPE"
    assert saved.data["findings"][0]["evidence_refs"] == [EVIDENCE]
    assert "sources" not in saved.data["findings"][0]
    assert saved.artifact_refs == (ARTIFACT,)


def test_resume_prompt_uses_frozen_model_contract_and_stable_labels(tmp_path):
    """原会話の差分続行でも保存契約を model へ戻さず、同じ短参照を使う。"""
    from skillmind.agent.continuation_prompt import continuation_prompt
    from skillmind.agent.task_brief import render_task_brief_prompt
    from tests.agent.test_continuation_prompt import context_with_brief

    source = context_with_brief(tmp_path)
    target = model_context(tmp_path)
    context = replace(
        source, task_snapshot=target.task_snapshot, result_schema=target.result_schema,
    )
    prompt = render_task_brief_prompt(
        context.task_brief, input_json=context.input_json, output_schema=context.result_schema,
        model_output_schema=context.task_snapshot["model_output_schema_json"],
    )
    context = replace(context, prompt=prompt)
    assert "repeat global reference lists" in prompt
    model_schema = json.loads(prompt.split("Output JSON Schema: ")[-1])
    assert "effects" not in model_schema["properties"]
    _, metadata = continuation_prompt(context, prompt, None)
    resumed, _ = continuation_prompt(context, prompt, metadata)
    assert resumed.startswith("Continue the same Run") and "Do not restart completed" in resumed


def test_projection_keeps_original_values_and_locates_nested_results():
    """工具列の各結果位置を示し、原 JSON/byte の値を短参照へ置換しない。"""
    original = {"steps": [{"response": {"evidence_refs": [EVIDENCE],
                                        "artifact_refs": [ARTIFACT], "content": "原文"}}]}
    copy = deepcopy(original)
    projected = project_result_references(original)
    assert original == copy and projected["steps"] == original["steps"]
    assert projected["result_references"][reference_label(EVIDENCE)] == (
        "/steps/0/response/evidence_refs/0"
    )
    assert reference_label(ARTIFACT) in projected["result_references"]
    assert project_result_references(copy)["result_references"] == projected["result_references"]


async def test_gateway_delivers_labels_without_changing_saved_tool_result(tmp_path):
    """実 Gateway の保存事実と SDK 向けの追加 metadata を明確に分離する。"""
    provider = CsvIssueProvider()
    registry = _registry(provider)
    context = model_context(tmp_path)
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    args = {"issue_ref": "fixture", "purpose": "Review"}
    await runtime.mcp.on_tool_authorized(context.tools[0].sdk_name, args, "call", str(uuid4()))
    response = await runtime.gateway.invoke_mcp(context.tools[0].sdk_name, args)
    delivered = json.loads(response["content"][0]["text"])
    ref = delivered["evidence_refs"][0]
    assert reference_label(ref) in delivered["result_references"]
    saved = writer.by_use_id["call"].result
    assert "result_references" not in saved and saved["evidence_refs"] == [ref]
    assert provider.calls == 1


async def test_child_assembly_reads_its_session_and_never_parent_proposals(tmp_path):
    """子の参照解決用 index が、親や別 branch の観測・提案を含まない SQL を使う。"""
    context = model_context(tmp_path)
    context = replace(context, permission_snapshot={**context.permission_snapshot,
                                                   "subagent_of": str(context.run_id)})
    session_id = uuid4()
    session = AsyncMock()
    rows = MagicMock()
    rows.all.return_value = [(EVIDENCE, None)]
    session.execute.return_value = rows
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    snapshot = await PostgresResultAssemblyLookup(factory).load(context, session_id)
    assert snapshot.evidence_refs == (EVIDENCE,) and not snapshot.proposal_refs
    assert session.execute.await_count == 1
    statement = session.execute.await_args.args[0]
    assert session_id in statement.compile().params.values()
    assert context.run_id in statement.compile().params.values()


async def test_audit_projection_retains_pending_and_cancelled_proposals(tmp_path):
    """未完了や取消を原提案一覧に残し、原回執がある操作だけを正確に表示する。"""
    context = model_context(tmp_path)
    session = AsyncMock()
    evidence = MagicMock()
    evidence.all.return_value = [(EVIDENCE, ARTIFACT), (OTHER, None)]
    proposals = MagicMock()
    proposals.all.return_value = [
        ("cp_applied", "APPLIED", "保存済み", EVIDENCE, OTHER),
        ("cp_pending", "PENDING_APPROVAL", "承認待ち", None, None),
        ("cp_cancelled", "CANCELLED", "取消済み", None, None),
    ]
    session.execute.side_effect = [evidence, proposals]
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    snapshot = await PostgresResultAssemblyLookup(factory).load(context, None)
    assert snapshot.proposal_refs == ("cp_applied", "cp_pending", "cp_cancelled")
    assert snapshot.effects == ({"proposal_ref": "cp_applied", "status": "APPLIED",
                                 "summary": "保存済み", "before_ref": EVIDENCE,
                                 "after_ref": OTHER},)
    assert snapshot.artifact_refs == (ARTIFACT,)
    assert session.execute.await_count == 2
