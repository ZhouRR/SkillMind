"""原文 task の Tool を model 宣言でなく選択資源から解決する回帰。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from jsonschema import ValidationError

from skillmind.effects.domain import ChangeProposalValidationError
from skillmind.effects.operation_policy import operation_authorization_key
from skillmind.effects.release import ExecutionFeatures
from skillmind.integrations.domain import (
    IntegrationStatus,
    ResolvedIntegration,
    ResourceBindingLevel,
    binding_checksum,
)
from skillmind.integrations.repository import IntegrationRepository
from skillmind.runs.domain import CreatedRun, CreateRunCommand, RunStatus
from skillmind.runs.effect_validation import validate_effect_intent
from skillmind.runs.repository import RunRepository
from skillmind.runs.service import RunService
from skillmind.skills.direct_candidate import candidate_schema
from skillmind.skills.resource_policy import default_capabilities, resource_operations
from tests.runs.test_task_run_service import _authorized_database, _resolved
from tests.skills.test_skill_service import CONTRACTS
from tests.skills.test_source_execution import compile_case, direct_case


@pytest.mark.parametrize("field", ["platform_tools", "capabilities", "operations", "access"])
def test_model_cannot_declare_tool_or_resource_permission(field: str) -> None:
    """許可リストを解析に戻さず、入力/接続選択だけを生成させる。"""
    from jsonschema import Draft202012Validator

    _, value = direct_case(writes=True)
    container = value if field == "platform_tools" else value["resource_requirements"][0]
    container[field] = [] if field != "access" else "write"
    with pytest.raises(ValidationError):
        Draft202012Validator(candidate_schema(CONTRACTS)).validate(value)


@pytest.mark.parametrize("writable", [False, True])
async def test_run_freezes_full_connection_scope_and_default_tools(writable: bool) -> None:
    """旧 Task binding の狭い scope に戻らず、資源本体の許可を原 Run に固定する。"""
    _, manifest = compile_case()
    requirement = {
        "key": "database", "kind": "other", "required": True, "selection": "resource",
        "accepted_providers": ["postgres"],
    }
    manifest["skill_execution"]["resource_requirements"] = [requirement]
    manifest["skill_execution"]["tasks"][0]["resource_keys"] = ["database"]
    resolved = _resolved()
    resolved = replace(
        resolved, task_key="execute", allowed_capabilities=tuple(default_capabilities()),
        skill_snapshot={**resolved.skill_snapshot, "manifest": manifest},
    )
    project, actor, run_id = uuid4(), uuid4(), uuid4()
    database = _authorized_database(project, actor, admin=True)
    capabilities = ("database.query/v1", "database.execute/v1") if writable else (
        "database.query/v1",
    )
    integration = ResolvedIntegration(
        integration_id=uuid4(), project_id=project, name="Fixture database", kind="other",
        provider="postgres", status=IntegrationStatus.ACTIVE, revision=1,
        capabilities=capabilities, scope={"statements": ["SELECT", "INSERT", "UPDATE", "DELETE"]
                                        if writable else ["SELECT"]},
        config={"access_mode": "native_sql"}, secret_reference_id=None,
    )
    configured = SimpleNamespace(
        integration_id=integration.integration_id, revision="1", binding_id=uuid4(),
        capability_version="database.query/v1", scope={"statements": ["SELECT"]},
    )
    captured: list[CreateRunCommand] = []

    async def create(self: RunRepository, command: CreateRunCommand) -> CreatedRun:
        """元の作成 transaction を通し、外部 I/O を伴う dispatch だけを差し替える。"""
        captured.append(command)
        return CreatedRun(run_id, project, command.task_id, RunStatus.QUEUED, 1,
                          datetime.now(UTC), False)

    async def freeze(self: IntegrationRepository, **kwargs):
        """実選択の全 scope で共有 checksum を作り、元 binding を返す。"""
        item = kwargs["binding"]
        checksum = binding_checksum(
            project_id=project, scope_level=ResourceBindingLevel.RUN, scope_key=str(run_id),
            requirement_key=item.requirement_key, resource_kind=item.resource_kind,
            integration_id=integration.integration_id, provider=integration.provider,
            capability_version=item.capability_version, revision="1", scope=dict(item.scope),
        )
        return SimpleNamespace(binding_id=uuid4(), checksum=checksum,
                               capability_version=item.capability_version)

    with (
        patch.object(RunRepository, "find_task_run_replay", new=AsyncMock(return_value=None)),
        patch.object(RunRepository, "create_idempotent", new=create),
        patch.object(RunRepository, "replace_initial_selected_sources", new=AsyncMock()) as saved,
        patch.object(IntegrationRepository, "find_configured_binding",
                     new=AsyncMock(return_value=configured)),
        patch.object(IntegrationRepository, "get_integration",
                     new=AsyncMock(return_value=integration)),
        patch.object(IntegrationRepository, "freeze_run_binding", new=freeze),
    ):
        await RunService(
            database.session_factory,
            execution_features=ExecutionFeatures(database_writes=True),
        ).create_task_run(
            project_id=project, resolved=resolved, input_json={}, sources={},
            idempotency_key="resource-policy-fixture", trace_id=None, actor_id=actor,
            authorization=database.access,
        )
    (command,) = captured
    source = command.selected_sources_json["database"]
    assert source["scope"] == integration.scope
    assert source["capabilities"] == list(capabilities)
    allowed = set(command.permission_snapshot_json["allowed_capabilities"])
    assert default_capabilities() <= allowed
    assert "database.query/v1" in allowed
    assert "database.execute/v1" not in allowed
    assert ("change.propose/v1" in allowed) is writable
    operations = command.permission_snapshot_json["resource_operations"]
    assert {op["operation"] for op in operations} == (
        {"INSERT", "UPDATE", "DELETE"} if writable else set()
    )
    frozen_source = saved.call_args.kwargs["selected_sources"]["database"]
    assert frozen_source["binding_capability"] == (
        "database.execute/v1" if writable else "database.query/v1"
    )


def test_http_operations_follow_access_level_and_keep_resource_ownership() -> None:
    """HTTP の方法を個別設定させず、別資源の operation を流用できない。"""
    sources = {"api": {"provider": "http", "capabilities": ["http.read/v1", "http.write/v1"],
                       "scope": {"base_urls": ["https://api.example.test"]}}}
    original = deepcopy(sources)
    ops = resource_operations(sources, allowed_capabilities={"http.read/v1", "http.write/v1"})
    assert {op["operation"] for op in ops} == {"POST", "PUT", "PATCH", "DELETE"}
    assert sources == original
    _, manifest = compile_case()
    arguments = {
        "effect_intent_key": operation_authorization_key("api", "http.write/v1", "POST"),
        "resource_key": "api", "operation": "POST", "capability_version": "http.write/v1",
        "risk_level": "HIGH", "resource_operations": ops,
    }
    assert validate_effect_intent(
        manifest["skill_execution"], **arguments,
    )["operation"] == "POST"
    for mutation in ({"resource_key": "other"}, {"operation": "TRACE"},
                     {"resource_operations": []}, {"risk_level": "LOW"}):
        with pytest.raises(ChangeProposalValidationError):
            validate_effect_intent(
                manifest["skill_execution"], **(arguments | mutation),
            )
