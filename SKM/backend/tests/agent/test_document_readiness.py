"""実 DB 読取と Gateway 契約を通し、ready と読み取り成功を区別する。"""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from skillmind.agent.context_builder import ContractStore, create_run_tool_registry
from skillmind.agent.document_readiness import DocumentReadinessProvider
from skillmind.db.models import EffectExecution
from tests.agent.test_document_provider import CONTRACTS, _context, _FakeSource
from tests.agent.test_tool_gateway import MemoryAuditWriter
from tests.documents.fakes import document_content
from tests.runs.test_document_prerequisites import prerequisite_case as prerequisite_case


@pytest.mark.parametrize("applied", [False, True])
async def test_readiness_reads_original_effects_and_replays_original_response(
    prerequisite_case, applied
):
    """未達状態も監査済み照会として返し、再生時に現在値へ差し替えない。"""
    case = prerequisite_case
    if not applied:
        case.rows[EffectExecution].status = "APPLYING"
    case.session.flush()
    context = _context(case.run.project_id)
    registry = create_run_tool_registry(
        ContractStore(CONTRACTS),
        document_source=_FakeSource(project_id=case.run.project_id, content=document_content()),
        document_readiness_provider=DocumentReadinessProvider(lambda: case.read),
    )
    registered = registry.resolve("document.readiness/v1", provider="platform", integration_id=None)
    run = replace(
        context.run,
        run_id=case.run.id,
        tools=(registered,),
        permission_snapshot={"allowed_capabilities": ["document.readiness/v1"]},
    )
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(run, audit_writer=writer)
    args = {"purpose": "Check the initial effect before document access"}
    session_id = str(uuid4())
    results = []
    for _ in range(2):
        await runtime.mcp.on_tool_authorized(registered.sdk_name, args, "check-once", session_id)
        response = await runtime.gateway.invoke_mcp(registered.sdk_name, args)
        assert not response.get("is_error"), response
        results.append(json.loads(response["content"][0]["text"]))
        case.rows[EffectExecution].status = "APPLIED"
        case.session.flush()
    assert results[0] == results[1]
    assert results[0]["ready"] is applied
    assert results[0]["required_effect_intents"] == ["register-run"]
    assert len(writer.completed) == 1
