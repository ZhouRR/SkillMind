"""Task Flow API の実認可・公開投影と、保存設計の静的 error 境界を検証する。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from projectmind.api.routes.task_flow import FlowContractResponse, get_task_flow_preview
from projectmind.auth.service import AuthenticatedActor
from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.projects import ProjectStatus, StoredProject
from projectmind.skills.domain import PublishedTaskNotFoundError, SkillVersionNotFoundError
from projectmind.skills.resource_binding import (
    ProjectResourceCandidate,
    TaskReadiness,
    evaluate_blueprint_readiness,
)
from projectmind.skills.service import TaskFlowPreviewResult
from projectmind.skills.task_flow_preview import (
    TaskFlowPreview,
    TaskFlowPreviewInvalidError,
    project_task_flow_preview,
)
from tests.api.fakes import (
    DeniedProjectAuthorizationService,
    FakeAuthService,
    FakeProjectAuthorizationService,
)
from tests.skills.task_flow_fixtures import make_task_flow_source
from tests.skills.test_task_flow_loading import FlowCatalog, FlowSession

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


class PreviewService:
    """公開 projection 反例用 port。認証は既存共有 fake と実 dependency を通す。"""

    def __init__(self) -> None:
        """完全な原 hash を持つ fixture を実 projector で生成する。"""

        self.source = make_task_flow_source()
        self.preview = project_task_flow_preview(
            source=self.source,
            task_key="explain",
            contracts_dir=CONTRACTS,
        )
        self.readiness: TaskReadiness | None = evaluate_blueprint_readiness(
            self.source.manifest["capability_blueprint"],
            candidates=(),
            registered_capabilities=frozenset(),
        )
        self.error: BaseException | None = None
        self.calls: list[dict[str, UUID | str]] = []

    async def get_task_flow_preview(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        skill_version_id: UUID,
        task_key: str,
    ) -> TaskFlowPreviewResult:
        """GET scope を記録し、指定された内部失敗または検証対象 DTO を返す。"""

        self.calls.append(
            {
                "organization_id": organization_id,
                "project_id": project_id,
                "skill_version_id": skill_version_id,
                "task_key": task_key,
            }
        )
        if self.error is not None:
            raise self.error
        return TaskFlowPreviewResult(self.preview, self.readiness)

    def path(self) -> str:
        """保存版の固定 identity を API URL にする。"""

        return (
            f"/api/v1/projects/{self.source.project_id}/skill-versions/"
            f"{self.source.skill_version_id}/tasks/explain/flow-preview"
        )

    def replace_payload(self, payload: dict[str, Any], *, rehash: bool = True) -> None:
        """Shape/帰属の破損を有効 checksum だけでは通せないことを検証する。"""

        if rehash:
            payload["preview_checksum"] = "sha256:" + sha256_hex(
                canonical_json(
                    {key: value for key, value in payload.items() if key != "preview_checksum"}
                )
            )
        self.preview = TaskFlowPreview(
            identity=payload["identity"],
            status=payload["status"],
            blueprint_checksum=payload["blueprint_checksum"],
            preview_checksum=payload["preview_checksum"],
            plan=payload["plan"],
            source_traces=tuple(payload["source_traces"]),
            preview_version=payload["preview_version"],
        )


class ArchivedProjects(FakeProjectAuthorizationService):
    """認可された archive は read 用に返し、write gate と混同しない。"""

    async def get_project(self, *, actor: AuthenticatedActor, project_id: UUID) -> StoredProject:
        """既存共有 scope fake の status だけを archive に変更する。"""

        return replace(
            await super().get_project(actor=actor, project_id=project_id),
            status=ProjectStatus.ARCHIVED,
        )


def _install(client: TestClient) -> PreviewService:
    """新 use case だけ差し替え、middleware/dependency/serializer は実物を使う。"""

    service = PreviewService()
    cast(FastAPI, client.app).state.skill_service = service
    return service


def test_available_preserves_original_optional_fields_and_exact_scope(client: TestClient) -> None:
    """公開 allowlist だけを返し、元 Task にない null/default を補わない。"""

    service = _install(client)
    response = client.get(service.path())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert {
        key: value for key, value in body.items() if key != "readiness"
    } == service.preview.to_json()
    assert (
        body["plan"]["task"]["value"] == service.source.manifest["capability_blueprint"]["tasks"][1]
    )
    assert "parameter_contract" not in body["plan"]["task"]["value"]
    assert body["readiness"]["scope"] == "SKILL_BLUEPRINT"
    assert len(body["readiness"]["assessment"]["requirements"]) == 3
    assert service.calls == [
        {
            "organization_id": cast(FastAPI, client.app).state.auth_service.actor.organization_id,
            "project_id": service.source.project_id,
            "skill_version_id": service.source.skill_version_id,
            "task_key": "explain",
        }
    ]


def test_actual_route_service_repository_projector_pipeline_is_read_only(
    client: TestClient,
) -> None:
    """実業務 chain が exact SQL/raw source/現在資源照合を通り、write seam を要求しない。"""

    session = FlowSession()
    auth: FakeAuthService = cast(FastAPI, client.app).state.auth_service
    auth.actor = replace(auth.actor, organization_id=session.organization_id)
    catalog = FlowCatalog()
    cast(FastAPI, client.app).state.skill_service = session.service(catalog)
    response = client.get(
        f"/api/v1/projects/{session.project_id}/skill-versions/{session.version.id}/tasks/explain/flow-preview"
    )
    assert response.status_code == 200
    assert response.json()["status"] == "AVAILABLE"
    assert len(session.queries) == 1
    assert len(session.loads) == 2
    assert catalog.calls == [session.project_id]
    assert session.closed


def test_archived_project_can_read_without_csrf_or_origin(client: TestClient) -> None:
    """既存 read 認可で archive を読み、GET に mutation CSRF を追加しない。"""

    service = _install(client)
    cast(FastAPI, client.app).state.project_service = ArchivedProjects()
    client.headers.pop("X-CSRF-Token")
    client.headers.pop("Origin")
    assert client.get(service.path()).status_code == 200


@pytest.mark.parametrize("denial", ["session", "project"])
def test_auth_failure_hides_preview_and_never_calls_service(
    client: TestClient, denial: str
) -> None:
    """原認可工場の 401/404 を維持し、失敗時は source 存在を観測しない。"""

    service = _install(client)
    if denial == "session":
        cast(FastAPI, client.app).state.auth_service = FakeAuthService(unauthorized=True)
    else:
        cast(FastAPI, client.app).state.project_service = DeniedProjectAuthorizationService()
    response = client.get(service.path())
    assert response.status_code == (401 if denial == "session" else 404)
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == (
        "authentication_required" if denial == "session" else "project_not_found"
    )
    assert service.calls == []


@pytest.mark.parametrize("bad_segment", ["project", "version", "task"])
def test_input_failure_is_no_store_and_does_not_load_source(
    client: TestClient, bad_segment: str
) -> None:
    """URI identity の書式違反を既存 422 Problem として返す。"""

    service = _install(client)
    path = service.path()
    if bad_segment == "project":
        path = path.replace(str(service.source.project_id), "not-uuid")
    elif bad_segment == "version":
        path = path.replace(str(service.source.skill_version_id), "not-uuid")
    else:
        path = path.replace("/explain/", "/INVALID/")
    response = client.get(path)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (SkillVersionNotFoundError("private source text"), 404, "task_flow_preview_not_found"),
        (PublishedTaskNotFoundError("private source text"), 404, "task_flow_preview_not_found"),
        (TaskFlowPreviewInvalidError(), 409, "task_flow_preview_invalid"),
        (SQLAlchemyError("private source text"), 503, "task_flow_preview_unavailable"),
        (ConnectionError("private source text"), 503, "task_flow_preview_unavailable"),
        (TimeoutError("private source text"), 503, "task_flow_preview_unavailable"),
    ],
)
def test_failures_have_static_problems_and_real_no_store(
    client: TestClient,
    error: Exception,
    status: int,
    code: str,
) -> None:
    """原因の SQL/source 本文を公開せず、HTTP 宣言と同じ status/header を返す。"""

    service = _install(client)
    service.error = error
    response = client.get(service.path())
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "private source text" not in response.text


@pytest.mark.parametrize(
    "change",
    [
        "project",
        "version",
        "task_id",
        "nil_skill",
        "version_length",
        "checksum",
        "extra_task",
        "optional_null",
        "required_bool",
        "effect_operation",
        "effect_risk",
        "empty_traces",
        "source_index_line",
        "resource_scope",
        "resource_duplicate",
        "resource_order",
        "undeclared_plan",
    ],
)
def test_public_projection_rejects_broken_or_foreign_saved_dto(
    client: TestClient, change: str
) -> None:
    """DTO が経路や白名単に違反したら、正しい形式の hash があっても静的 409 にする。"""

    service = _install(client)
    value = service.preview.to_json()
    if change in {"project", "version", "task_id", "nil_skill"}:
        field = {
            "project": "project_id",
            "version": "skill_version_id",
            "task_id": "task_id",
            "nil_skill": "skill_id",
        }[change]
        value["identity"][field] = str(UUID(int=0) if change == "nil_skill" else uuid4())
    elif change == "version_length":
        value["identity"]["version"] = "1" * 33
    elif change == "checksum":
        value["preview_checksum"] = "sha256:" + "0" * 64
    elif change == "extra_task":
        value["plan"]["task"]["value"]["private_field"] = "private source text"
    elif change == "optional_null":
        value["plan"]["task"]["value"]["parameter_contract"] = None
    elif change == "required_bool":
        value["plan"]["task_resources"][0]["value"]["required"] = 1
    elif change in {"effect_operation", "effect_risk"}:
        value["plan"]["shared"]["effect_intents"][0].pop(change.removeprefix("effect_"))
    elif change == "empty_traces":
        value["source_traces"] = []
    elif change == "source_index_line":
        value["source_traces"][0]["verification"] = "SOURCE_INDEX"
    elif change == "resource_scope":
        value["plan"]["task_resources"][0]["scope"] = "SKILL"
    elif change == "resource_duplicate":
        value["plan"]["shared"]["resource_requirements"].append(
            deepcopy(value["plan"]["shared"]["resource_requirements"][0])
        )
    elif change == "resource_order":
        value["plan"]["task_resources"].reverse()
    else:
        value["status"] = "NOT_DECLARED"
    service.replace_payload(value, rehash=change != "checksum")
    response = client.get(service.path())
    assert response.status_code == 409
    assert response.json()["code"] == "task_flow_preview_invalid"
    assert "private source text" not in response.text


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "key",
        "kind",
        "required",
        "access",
        "capabilities",
        "selection_guidance",
    ],
)
def test_readiness_cannot_change_or_omit_blueprint_declarations(
    client: TestClient, change: str
) -> None:
    """現在の候補評価は原要求の全数と意味を守り、選択 Task だけに縮小しない。"""

    service = _install(client)
    assert service.readiness is not None
    requirements = list(service.readiness.requirements)
    if change == "missing":
        requirements.pop()
    elif change == "duplicate":
        requirements[1] = requirements[0]
    else:
        changes: dict[str, Any] = {
            "key": "different",
            "kind": "repository",
            "required": False,
            "access": "write",
            "capabilities": ("document.read/v1",),
            "selection_guidance": "different",
        }
        requirements[0] = replace(requirements[0], **{change: changes[change]})
    service.readiness = replace(service.readiness, requirements=tuple(requirements))
    response = client.get(service.path())
    assert response.status_code == 409
    assert response.json()["code"] == "task_flow_preview_invalid"


def test_candidate_private_scope_never_reaches_public_projection(client: TestClient) -> None:
    """readiness の公開項目を限定し、現在候補の内部 locator や revision を落とす。"""

    service = _install(client)
    assert service.readiness is not None
    candidate = ProjectResourceCandidate(
        key="doc-1",
        kind="document",
        provider="documents",
        label="Known document",
        capabilities=(),
        integration_id=uuid4(),
        revision="private-revision",
        scope={"endpoint": "private source text"},
    )
    service.readiness = replace(
        service.readiness,
        requirements=tuple(
            replace(item, candidates=(candidate,)) if item.key == "source" else item
            for item in service.readiness.requirements
        ),
    )
    response = client.get(service.path())
    assert response.status_code == 200
    assert "private" not in response.text
    public = response.json()["readiness"]["assessment"]["requirements"][0]["candidates"][0]
    assert set(public) == {"key", "kind", "provider", "label"}


def test_not_declared_requires_absent_assessment(client: TestClient) -> None:
    """未宣言に readiness を付けた内部不整合を既知の設計として公開しない。"""

    service = _install(client)
    value = service.preview.to_json()
    value.update(status="NOT_DECLARED", plan=None, blueprint_checksum=None, source_traces=[])
    service.replace_payload(value)
    assert client.get(service.path()).status_code == 409
    service.readiness = None
    response = client.get(service.path())
    assert response.status_code == 200
    assert response.json()["readiness"] == {"scope": "SKILL_BLUEPRINT", "assessment": None}


@pytest.mark.parametrize(
    "change", ["duplicate_enum", "wrong_enum_type", "duplicate_field", "depth", "keyword"]
)
def test_original_contract_compiler_rejects_semantically_invalid_dto(
    client: TestClient,
    change: str,
) -> None:
    """公開型の形だけでは通る契約破損も、既存 compiler の同じ規則で拒否する。"""

    service = _install(client)
    value = service.preview.to_json()
    draft: dict[str, Any] = {
        "contract_version": "projectmind.task-contract-draft/v1",
        "type": "string",
    }
    if change == "duplicate_enum":
        draft["enum"] = ["same", "same"]
    elif change == "wrong_enum_type":
        draft["enum"] = [1]
    elif change == "duplicate_field":
        draft.update(
            type="object",
            fields=[
                {"key": "same", "type": "string", "required": True},
                {"key": "same", "type": "string", "required": False},
            ],
        )
    elif change == "depth":
        draft["type"] = "array"
        node = draft
        for _ in range(6):
            node["items"] = {"type": "array"}
            node = node["items"]
        node["items"] = {"type": "string"}
    else:
        draft["minimum"] = 3
    value["plan"]["task"]["value"]["parameter_contract"] = draft
    service.replace_payload(value)
    response = client.get(service.path())
    assert response.status_code == 409
    assert response.json()["code"] == "task_flow_preview_invalid"


def test_original_valid_recursive_contract_is_preserved(client: TestClient) -> None:
    """有効な再帰型と false/number enum を補正せず、原 JSON のまま公開する。"""

    service = _install(client)
    value = service.preview.to_json()
    draft = {
        "contract_version": "projectmind.task-contract-draft/v1",
        "type": "object",
        "fields": [
            {"key": "enabled", "type": "boolean", "required": False, "enum": [False, True]},
            {
                "key": "values",
                "type": "array",
                "required": True,
                "items": {"type": "number", "minimum": 0, "maximum": 2, "enum": [0, 1.5]},
            },
        ],
    }
    value["plan"]["task"]["value"]["parameter_contract"] = draft
    service.replace_payload(value)
    response = client.get(service.path())
    assert response.status_code == 200
    assert response.json()["plan"]["task"]["value"]["parameter_contract"] == draft


@pytest.mark.parametrize("position", ["root", "field", "items"])
def test_numeric_enum_equivalence_matches_original_and_public_schema(
    client: TestClient,
    position: str,
) -> None:
    """canonical 文字列が異なる 1/1.0 も、根と再帰位置で同じ Schema 重複として拒否する。"""

    number: dict[str, Any] = {"type": "number", "enum": [1, 1.0]}
    draft: dict[str, Any] = {"contract_version": "projectmind.task-contract-draft/v1"}
    if position == "root":
        draft.update(number)
    elif position == "field":
        draft.update(type="object", fields=[{"key": "value", "required": True, **number}])
    else:
        draft.update(
            type="object",
            fields=[
                {
                    "key": "values",
                    "required": True,
                    "type": "array",
                    "items": number,
                }
            ],
        )
    original = json.loads((CONTRACTS / "capability-blueprint" / "v1.schema.json").read_text())
    original_draft = {"$ref": "#/$defs/taskContractDraft", "$defs": original["$defs"]}
    assert not Draft202012Validator(original_draft).is_valid(draft)
    assert not Draft202012Validator(FlowContractResponse.model_json_schema()).is_valid(draft)
    with pytest.raises(ValidationError):
        FlowContractResponse.model_validate_json(json.dumps(draft))
    service = _install(client)
    value = service.preview.to_json()
    value["plan"]["task"]["value"]["parameter_contract"] = draft
    service.replace_payload(value)
    response = client.get(service.path())
    assert response.status_code == 409
    assert response.json()["code"] == "task_flow_preview_invalid"


@pytest.mark.asyncio
async def test_cancelled_error_is_not_converted_to_storage_problem(client: TestClient) -> None:
    """取消しは BaseException のまま伝播し、通常の保存障害回执へ読み替えない。"""

    service = _install(client)
    service.error = asyncio.CancelledError()
    request = Request({"type": "http", "app": client.app})
    with pytest.raises(asyncio.CancelledError):
        await get_task_flow_preview(
            request,
            Response(),
            service.source.project_id,
            service.source.skill_version_id,
            cast(FastAPI, client.app).state.auth_service.actor,
            "explain",
        )
