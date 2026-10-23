"""原生 SQL の原 commit 応答を、監査組立と最終保存まで同じ契約で検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest

from skillmind.agent.domain import AgentEventType
from skillmind.agent.model_result import PostgresResultAssemblyLookup
from skillmind.agent.result_validation import ResultValidator
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.postgres_native import SQL_VERSION, SQL_WRITE, NativeDatabaseWriteProvider
from skillmind.effects.projections import effect_evidence_row
from skillmind.runs.domain import RunStatus
from skillmind.worker.executor import AgentRunExecutor
from tests.agent.test_model_result import model_context
from tests.agent.test_result_effect_lookup import _claimed, _graph, _invalid, _lookup
from tests.agent.test_result_references import ProposalLookup
from tests.agent.test_result_validation import MemoryEvidenceLookup
from tests.effects.test_native_resource_effects import Connection
from tests.runs.creation_fakes import creation_command, creation_intent, stored_creation
from tests.worker.test_agent_run_executor import SequenceEngine, _event, _service
from tests.worker.test_agent_run_executor import _claimed as claimed_run


async def sql_graph(monkeypatch, *, operation="UPDATE", count=1):
    """実 Provider と Evidence writer を通し、DB 接続と command 応答だけを置き換える。"""
    graph = _graph()
    graph.proposal.capability_version = SQL_WRITE
    graph.proposal.operation = operation
    graph.proposal.summary = "SQL transaction recorded"
    graph.proposal.verification_json = {"method": "SQL_RESPONSE", "paths": ["/statement"]}
    graph.execution.provider = graph.tool.provider = "postgres"
    graph.execution.provider_version = SQL_VERSION
    graph.tool.capability_version = SQL_WRITE
    sql = {
        "INSERT": "INSERT INTO public.items(id) VALUES ($1)",
        "UPDATE": "UPDATE public.items SET done=true WHERE id=$1",
        "DELETE": "DELETE FROM public.items WHERE id=$1",
    }[operation]
    original = replace(_claimed(graph), operation=operation,
        target={"locator": "database"}, precondition={},
        integration_config={"access_mode": "native_sql"},
        integration_scope={"statements": [operation]},
        changes=({"path": "/statement", "action": "SET", "value": {
            "statement": {"sql": sql, "parameters": [1]},
        }},))
    connection = Connection()
    connection.driver.execute.return_value = (
        f"INSERT 0 {count}" if operation == "INSERT" else f"{operation} {count}"
    )
    engine = SimpleNamespace(connect=lambda: connection, dispose=AsyncMock())
    monkeypatch.setattr("skillmind.effects.postgres_native.create_database_engine",
        Mock(return_value=engine))
    result = await NativeDatabaseWriteProvider(AsyncMock()).apply(
        original, credential="synthetic-test-only",
    )
    graph.execution.verification_json = dict(result.verification)
    graph.tool.result_json.update(provider="postgres", verification=dict(result.verification))
    before, after = [effect_evidence_row(
        run_id=graph.proposal.run_id, tool_call_id=graph.tool.id,
        evidence_ref=ref, draft=draft, now=graph.execution.executed_at,
    ) for ref, draft in ((graph.execution.before_ref, result.before),
                        (graph.execution.after_ref, result.after))]
    return replace(graph, before=before, after=after), connection


def result_validator(graph, *, run=None, failed=None):
    """DB の行応答だけを fake にし、参照解決・監査組立・Effect 照合は本物を使う。"""
    records = [graph] + ([failed] if failed is not None else [])
    joined = [(*g.joined(omit=frozenset({"before", "after"}) if g is failed else frozenset()),
               run) for g in records]
    lookup, factory, session = _lookup(*joined)
    references = [(graph.before.evidence_ref, None), (graph.after.evidence_ref, None)]
    projections = [(g.proposal.proposal_ref, g.proposal.status, g.proposal.summary,
                    g.execution.before_ref, g.execution.after_ref) for g in records]
    results = []
    for rows in (references, projections, joined):
        response = MagicMock()
        response.all.return_value = rows
        results.append(response)
    session.execute.side_effect = results
    return ResultValidator(
        MemoryEvidenceLookup(frozenset(ref for ref, _ in references)),
        ProposalLookup(graph.proposal.run_id), effect_lookup=lookup,
        assembly_lookup=PostgresResultAssemblyLookup(factory),
    )


def partial_result():
    """モデルに監査 field を再生成させず、未実行範囲を残した業務結果を返す。"""
    return {"summary": "Operations recorded; some cases were not run.", "status": "PARTIAL",
        "deliverables": [{"key": "summary", "kind": "report", "title": "Execution summary",
                          "content": "Original records are retained."}],
        "findings": [], "open_questions": [], "limitations": ["Some cases were not run."],
        "confidence": 0.8, "needs_review": True}


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE", "DELETE"])
@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("approval_source", ["USER", "RUN_START"])
async def test_native_sql_passes_assembled_partial_outcome(
    tmp_path, monkeypatch, operation, count, approval_source,
):
    """原 SQL を一度だけ実行し、0 行や自動承認も最終 PARTIAL として保存可能にする。"""
    graph, connection = await sql_graph(monkeypatch, operation=operation, count=count)
    run = None
    if approval_source == "RUN_START":
        graph.approval.source = approval_source
        intent = replace(creation_intent(), project_id=graph.project_id,
            actor_id=graph.approval.actor_id, auto_approve=True)
        run = stored_creation(creation_command(intent))
        run.id = graph.proposal.run_id
    context = replace(model_context(tmp_path), run_id=graph.proposal.run_id,
        project_id=graph.project_id)
    candidate = partial_result()
    validated = await result_validator(graph, run=run).validate_context(
        context, structured_output=candidate,
    )
    assert validated.data["status"] == "PARTIAL"
    assert validated.data["limitations"] == candidate["limitations"]
    assert validated.data["effects"][0]["status"] == "APPLIED"
    assert validated.change_proposal_refs == {graph.proposal.proposal_ref}
    assert graph.execution.verification_json["business_verdict"] == "NOT_EVALUATED"
    assert connection.committed and connection.driver.execute.await_count == 1
    assert connection.commands == [
        "SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '2s'",
    ]


@pytest.mark.parametrize("damage", [
    "missing_commit", "false_commit", "numeric_commit", "wrong_method", "expected_method",
    "wrong_paths", "missing_tag", "wrong_command", "wrong_count", "boolean_count",
    "negative_count", "unknown_result", "wrong_approval", "another_run", "broken_hash",
    "changed_tool_receipt",
])
async def test_sql_response_requires_original_committed_receipt(monkeypatch, damage):
    """SQL_RESPONSE の追加でも未確認 commit・壊れた応答・別の原操作を受理しない。"""
    graph, _ = await sql_graph(monkeypatch)
    verification = deepcopy(graph.execution.verification_json)
    snapshot = graph.after.metadata_json["snapshot"]
    if damage == "missing_commit":
        verification.pop("committed")
    elif damage == "false_commit":
        verification["committed"] = False
    elif damage == "numeric_commit":
        verification["committed"] = 1
    elif damage == "wrong_method":
        verification["method"] = "READ_BACK"
    elif damage == "expected_method":
        graph.proposal.verification_json["method"] = "READ_BACK"
    elif damage == "wrong_paths":
        graph.proposal.verification_json["paths"] = verification["matched_paths"] = ["/other"]
    elif damage == "missing_tag":
        snapshot.pop("command_tag")
    elif damage == "wrong_command":
        snapshot["command_tag"] = "DELETE 1"
    elif damage == "wrong_count":
        snapshot["affected_rows"] = 2
    elif damage == "boolean_count":
        snapshot["affected_rows"] = True
    elif damage == "negative_count":
        snapshot["affected_rows"] = -1
    elif damage == "unknown_result":
        graph.execution.error_json = {"code": "effect_result_unknown"}
    elif damage == "wrong_approval":
        graph.approval.proposal_checksum = "sha256:" + "0" * 64
    elif damage == "another_run":
        graph.after.run_id = uuid4()
    graph.after.content_hash = "sha256:" + sha256_hex(canonical_json(snapshot))
    if damage == "broken_hash":
        graph.after.content_hash = "sha256:" + "0" * 64
    graph.execution.verification_json = verification
    graph.tool.result_json["verification"] = deepcopy(verification)
    if damage == "changed_tool_receipt":
        graph.tool.result_json["verification"]["committed"] = False
    assert await _invalid(graph) == {graph.proposal.proposal_ref}


async def test_sql_response_is_not_accepted_for_other_capabilities():
    """SQL の commit 応答を他資源の READ_BACK へ流用しない。"""
    graph = _graph()
    graph.proposal.verification_json["method"] = "SQL_RESPONSE"
    graph.execution.verification_json.update(method="SQL_RESPONSE", committed=True)
    graph.tool.result_json["verification"] = dict(graph.execution.verification_json)
    assert await _invalid(graph) == {graph.proposal.proposal_ref}


async def test_worker_saves_partial_report_with_committed_and_rolled_back_sql(
    tmp_path, monkeypatch,
):
    """確定した失敗を残しつつ、主 Worker が PARTIAL の原報告を終端保存する。"""
    graph, connection = await sql_graph(monkeypatch)
    failed, _ = await sql_graph(monkeypatch)
    failed.proposal.run_id = failed.execution.run_id = failed.tool.run_id = graph.proposal.run_id
    failed.approval.run_id = graph.proposal.run_id
    failed.proposal.project_id = graph.project_id
    failed = replace(failed, project_id=graph.project_id)
    failed.proposal.status = failed.execution.status = failed.tool.status = "FAILED"
    failed.execution.before_ref = failed.execution.after_ref = None
    failed.execution.error_json = {"code": "sql_invalid_parameter", "retryable": False}
    failed.execution.verification_json = {}
    failed.tool.result_json = None
    failed.tool.error_json = {"code": "invalid_request", "retryable": False}
    claimed = replace(claimed_run(), run_id=graph.proposal.run_id, project_id=graph.project_id)
    context = replace(model_context(tmp_path), run_id=claimed.run_id,
        run_attempt_id=claimed.run_attempt_id, project_id=claimed.project_id,
        user_id=claimed.actor_id, sequence_start=10)
    builder = MagicMock(build=AsyncMock(return_value=context))
    service = _service()
    event = _event(claimed, 10, AgentEventType.RESULT_COMPLETED,
        payload={"structured_output": partial_result()}, session_id=str(uuid4()))
    await AgentRunExecutor(run_service=service, context_builder=builder,
        engine=SequenceEngine([event]), result_validator=result_validator(graph, failed=failed),
        lease_seconds=60).execute(claimed)
    terminal = service.finalize_execution.await_args.kwargs
    assert terminal["target"] is RunStatus.SUCCEEDED, terminal.get("error_json")
    assert terminal["result"].data["status"] == "PARTIAL"
    assert {effect["status"] for effect in terminal["result"].data["effects"]} == {
        "APPLIED", "FAILED",
    }
    assert connection.driver.execute.await_count == 1
