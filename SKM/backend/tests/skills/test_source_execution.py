"""新候補から原文実行・受控書込まで、旧 Blueprint を生成せず接続する。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from skillmind.agent.domain import RunLimits
from skillmind.agent.task_brief import build_agent_task_brief, render_task_brief_prompt
from skillmind.api.routes.task_flow import TaskFlowPreviewResponse
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.domain import ChangeProposalValidationError
from skillmind.effects.operation_policy import operation_authorization_key
from skillmind.effects.proposal import parse_change_proposal_request
from skillmind.effects.release import ExecutionFeatures
from skillmind.runs.repository_effects import EffectOperationsMixin
from skillmind.skills.direct_candidate import candidate_schema
from skillmind.skills.execution import resolve_skill_definition, validate_execution
from skillmind.skills.interpreter import InterpreterFixtureRunner, load_interpreter_system_skill
from skillmind.skills.task_catalog import project_published_tasks
from skillmind.skills.task_flow_preview import TaskFlowPreviewSource, project_task_flow_preview
from tests.effects.test_document_write import request as proposal_request
from tests.skills.test_candidate import request as legacy_request
from tests.skills.test_skill_service import CONTRACTS, SYSTEM_SKILL


def direct_case(*, writes: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """凍結 source と解釈 candidate は別物として準備する。"""
    request = legacy_request()
    request["interpreter"] = load_interpreter_system_skill(
        SYSTEM_SKILL,
        generation_schema=candidate_schema(CONTRACTS),
    ).to_dict()
    candidate = json.loads((CONTRACTS / "examples/skill-candidate.v2.json").read_text())
    if writes:
        candidate["resource_requirements"] = [
            {
                "key": "review_outputs",
                "kind": "document",
                "required": True,
                "access": "write",
                "capabilities": ["document.write/v1"],
                "accepted_providers": ["project-library"],
                "selection_guidance": None,
                "source_ref": "s0:1",
                "operations": [{"capability_version": "document.write/v1", "operation": "CREATE"}],
            }
        ]
    return request, candidate


def compile_case(*, writes: bool = False):
    """実 runner の source/hash 照合と publish preflight を通す。"""
    request, candidate = direct_case(writes=writes)
    response = InterpreterFixtureRunner(CONTRACTS).run(
        request,
        candidate,
        bind_identity=True,
        require_direct_candidate=True,
    )
    return request, response["runtime_manifest_draft"]


def test_direct_candidate_publish_catalog_brief_preview_roundtrip() -> None:
    """Source の複製/再解釈なしに caller 契約と実行可能な一 task を公開する。"""
    request, manifest = compile_case(writes=True)
    assert "capability_blueprint" not in manifest
    assert manifest["source_documents"] == request["source"]["source_documents"]
    assert "output_contract" not in manifest["tasks"][0]
    version_id, project_id = uuid4(), uuid4()
    descriptors = project_published_tasks(
        skill_id=uuid4(),
        skill_version_id=version_id,
        skill_key=manifest["identity"]["skill_key"],
        skill_name="Review",
        version="0.1.0",
        published_at=None,
        manifest=manifest,
    )
    assert len(descriptors) == 1
    task = manifest["tasks"][0]
    snapshot = {
        **task,
        "task_key": task["key"],
        "skill_version_id": str(version_id),
        "manifest_checksum": "sha256:" + sha256_hex(canonical_json(manifest)),
        "output_schema_checksum": descriptors[0].output_schema_checksum,
    }
    brief = build_agent_task_brief(
        run_id=uuid4(),
        project_id=project_id,
        task_snapshot=snapshot,
        manifest=manifest,
        selected_sources={},
        tools=[],
        limits=RunLimits(max_turns=20, wall_timeout_seconds=300, max_output_bytes=100000),
    )
    schema = json.loads((CONTRACTS / "agent-task-brief/v2.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(brief.brief)
    assert "guidance" not in brief.brief and "objective" not in brief.brief
    assert brief.brief["source_documents"] == manifest["source_documents"]
    prompt = render_task_brief_prompt(
        brief.brief, input_json={}, output_schema=descriptors[0].output_schema
    )
    assert "Omit effect_intent_key" in prompt
    assert "HTML" in prompt
    source = TaskFlowPreviewSource(
        project_id=project_id,
        skill_id=descriptors[0].skill_id,
        skill_version_id=version_id,
        skill_key=manifest["identity"]["skill_key"],
        version="0.1.0",
        manifest_checksum=snapshot["manifest_checksum"],
        manifest=manifest,
        skill_source_id=uuid4(),
        source_hash=request["source"]["content_hash"],
        source_file_index=request["source"]["normalized_package"]["source"]["files"],
        source_snapshot=[
            {"path": d["path"], "content": d["content"]} for d in manifest["source_documents"]
        ],
        interpretation_id=UUID(manifest["identity"]["interpretation_id"]),
        interpreter_version=manifest["identity"]["interpreter_version"],
    )
    preview = project_task_flow_preview(source=source, task_key="execute", contracts_dir=CONTRACTS)
    body = {**preview.to_json(), "readiness": {"scope": "SKILL_BLUEPRINT", "assessment": None}}
    validated = TaskFlowPreviewResponse.model_validate_json(json.dumps(body))
    assert validated.status == "SOURCE_EXECUTION" and validated.plan is None
    assert validated.source_execution is not None
    assert ExecutionFeatures(document_writes=True).blueprint_enabled(manifest["skill_execution"])
    assert not ExecutionFeatures().blueprint_enabled(manifest["skill_execution"])


def test_source_operations_propose_without_intent_and_preserve_idempotency() -> None:
    """既存 Proposal へ操作識別子を正規化して渡し、承認/適用の同じ照合を通す。"""
    _, manifest = compile_case(writes=True)
    payload = proposal_request()
    del payload["effect_intent_key"]
    draft = parse_change_proposal_request(payload)
    assert draft.request_fingerprint == sha256_hex(canonical_json(payload))
    assert draft.effect_intent_key == operation_authorization_key(
        "review_outputs", "document.write/v1", "CREATE"
    )
    arguments = dict(
        effect_intent_key=draft.effect_intent_key,
        resource_key=draft.resource_key,
        capability_version=draft.capability_version,
        operation=draft.operation,
        risk_level="MEDIUM",
    )
    validate = EffectOperationsMixin._validate_effect_intent
    assert validate(manifest["skill_execution"], **arguments)["operation"] == "CREATE"
    for change in (
        {"resource_key": "another"},
        {"operation": "UPDATE"},
        {"risk_level": "LOW"},
        {"capability_version": "database.write/v1"},
        {"effect_intent_key": "save_document"},
    ):
        with pytest.raises(ChangeProposalValidationError):
            validate(manifest["skill_execution"], **(arguments | change))
    with pytest.raises(ChangeProposalValidationError):
        validate({"effect_intents": []}, **arguments)


@pytest.mark.parametrize("mutation", ["unknown", "mixed", "two_tasks", "undeclared_operation"])
def test_invalid_execution_is_not_silently_downgraded(mutation: str) -> None:
    """不明版と権限参照の不整合は閉じて拒否する。"""
    _, manifest = compile_case(writes=True)
    definition = manifest["skill_execution"]
    if mutation == "unknown":
        definition["execution_version"] = "unknown"
    elif mutation == "mixed":
        manifest["capability_blueprint"] = {}
    elif mutation == "two_tasks":
        definition["tasks"].append(deepcopy(definition["tasks"][0]))
    else:
        definition["resource_requirements"][0]["operations"][0]["operation"] = "DELETE"
    with pytest.raises((ValueError, ValidationError)):
        resolved = resolve_skill_definition(manifest)
        assert resolved is not None
        validate_execution(resolved, CONTRACTS)


def test_optional_read_resource_does_not_become_required_tool() -> None:
    """接続なしの任意情報源は起動の必須 Tool に昇格させない。"""
    request, candidate = direct_case()
    candidate["resource_requirements"] = [
        {
            "key": "references",
            "kind": "repository",
            "required": False,
            "access": "read",
            "capabilities": ["repository.read/v1"],
            "accepted_providers": ["git"],
            "selection_guidance": None,
            "source_ref": "s0:1",
            "operations": [],
        }
    ]
    response = InterpreterFixtureRunner(CONTRACTS).run(
        request, candidate, bind_identity=True, require_direct_candidate=True
    )
    tools = response["runtime_manifest_draft"]["tools"]
    assert next(t for t in tools if t["capability"] == "repository.read/v1")["required"] is False


@pytest.mark.parametrize("mutation", [None, "operation", "scope", "permission", "approval"])
async def test_source_execution_keeps_effect_step_authorization(mutation: str | None) -> None:
    """新宣言でも批准/lease/元版/接続の実 repository 再検査を省略しない。"""
    from skillmind.effects.domain import EffectLeaseValidationError
    from tests.runs.effect_authorization_harness import AuthorizationHarness

    h = AuthorizationHarness()
    task = h.run.task_snapshot_json
    snapshot = task["skill_snapshots"][0]
    legacy = snapshot["manifest"].pop("capability_blueprint")
    resource = legacy["resource_requirements"][0]
    resource["operations"] = [
        {"capability_version": h.proposal.capability_version, "operation": h.proposal.operation}
    ]
    snapshot["manifest"]["skill_execution"] = {
        "execution_version": "skillmind.skill-execution/v1",
        "resource_requirements": [resource],
        "tasks": [],
        "source_traces": [],
    }
    h.proposal.effect_intent_key = operation_authorization_key(
        resource["key"], h.proposal.capability_version, h.proposal.operation
    )
    if mutation == "operation":
        resource["operations"][0]["operation"] = "UPDATE"
    elif mutation == "scope":
        resource["access"] = "read"
    elif mutation == "permission":
        h.integration.revision += 1
    elif mutation == "approval":
        h.approval.decision = "REJECTED"
    snapshot["manifest_checksum"] = "sha256:" + sha256_hex(canonical_json(snapshot["manifest"]))
    task["manifest_checksum"] = snapshot["manifest_checksum"]
    if mutation:
        with pytest.raises(EffectLeaseValidationError):
            await h.authorize()
    else:
        await h.authorize()
