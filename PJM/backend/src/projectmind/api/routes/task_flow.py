"""精確な公開 SkillVersion の Task 設計を、実行権や実行事実を作らず読み取る。"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import SQLAlchemyError

from projectmind.api.auth_dependencies import ProjectReadActor
from projectmind.api.problems import ProblemException, problem_openapi_response
from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.runs.domain import derive_task_id
from projectmind.skills.domain import PublishedTaskNotFoundError, SkillVersionNotFoundError
from projectmind.skills.resource_binding import TaskReadiness
from projectmind.skills.service import SkillService, TaskFlowPreviewResult
from projectmind.skills.task_contract import compile_task_contract
from projectmind.skills.task_flow_preview import TaskFlowPreview, TaskFlowPreviewInvalidError

router = APIRouter()
_HEADERS = {"Cache-Control": "no-store"}
_OPENAPI_HEADERS = {"Cache-Control": {"schema": {"type": "string", "const": "no-store"}}}
_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: problem_openapi_response(description, headers=_OPENAPI_HEADERS)
    for code, description in (
        (401, "Authentication is required"),
        (403, "Project access is forbidden"),
        (404, "project_not_found or task_flow_preview_not_found"),
        (409, "task_flow_preview_invalid: saved design or source failed verification"),
        (422, "Invalid Project, SkillVersion, or Task identity"),
        (503, "task_flow_preview_unavailable: saved preview storage is unavailable"),
    )
}

Key = Annotated[str, Field(strict=True, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=1000)]
Checksum = Annotated[str, Field(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
Capability = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_.-]*/v[1-9][0-9]*$")]
ResourceKind = Literal["issue", "repository", "document", "file", "knowledge", "other"]
ContractValueType = Literal["object", "array", "string", "integer", "number", "boolean"]
ContractScalar = str | int | float | bool


def _flow_schema(schema: dict[str, Any]) -> None:
    """原 Blueprint の optional は省略だけを許し、明示 null を公開契約へ増やさない。"""

    required = schema.get("required", [])
    for name, value in schema.get("properties", {}).items():
        if name in required:
            continue
        variants = value.get("anyOf")
        if isinstance(variants, list):
            remaining = [item for item in variants if item != {"type": "null"}]
            if len(remaining) != len(variants):
                if len(remaining) == 1:
                    value.pop("anyOf")
                    value.update(remaining[0])
                else:
                    value["anyOf"] = remaining
                value.pop("default", None)


def _preview_schema(schema: dict[str, Any]) -> None:
    """公開 state の組合せを Schema にも記録し、validator だけの暗黙規則にしない。"""

    _flow_schema(schema)
    schema["allOf"] = [
        {
            "if": {"properties": {"status": {"const": "AVAILABLE"}}, "required": ["status"]},
            "then": {
                "properties": {
                    "plan": {"type": "object"},
                    "blueprint_checksum": {"type": "string"},
                    "source_traces": {"minItems": 1},
                }
            },
            "else": {
                "properties": {
                    "plan": {"type": "null"},
                    "blueprint_checksum": {"type": "null"},
                    "source_traces": {"maxItems": 0},
                    "readiness": {"properties": {"assessment": {"type": "null"}}},
                }
            },
        }
    ]


class _FlowModel(BaseModel):
    """型変換による捏造や内部列の追加公開を避ける、公開 field の共通境界。"""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        json_schema_extra=_flow_schema,
    )

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_optional_null(cls, value: Any) -> Any:
        """省略用の内部 default と、原 JSON に明示された不正 null を区別する。"""

        if isinstance(value, dict) and any(
            value.get(name, object()) is None and not field.is_required()
            for name, field in cls.model_fields.items()
        ):
            raise ValueError("Optional declarations must not be null")
        return value


class FlowNoteResponse(_FlowModel):
    """原 guidance の識別子と本文だけを表示する。"""

    key: Key
    text: Text


class FlowContractItemResponse(_FlowModel):
    """既存 TaskContractDraft の再帰値を明示 field だけへ写す。"""

    type: ContractValueType
    description: Annotated[str, Field(strict=True, min_length=1, max_length=500)] | None = None
    enum: (
        Annotated[
            list[ContractScalar],
            Field(min_length=1, max_length=50, json_schema_extra={"uniqueItems": True}),
        ]
        | None
    ) = None
    fields: Annotated[list[FlowContractFieldResponse], Field(max_length=100)] | None = None
    items: FlowContractItemResponse | None = None
    min_length: Annotated[int, Field(strict=True, ge=0, le=100000)] | None = None
    max_length: Annotated[int, Field(strict=True, ge=0, le=100000)] | None = None
    pattern: Annotated[str, Field(strict=True, min_length=1, max_length=128)] | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None


class FlowContractFieldResponse(FlowContractItemResponse):
    """入力・出力 field の原 required を保持し、画面側の既定値で増やさない。"""

    key: Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    required: Annotated[bool, Field(strict=True)]


class FlowContractResponse(FlowContractItemResponse):
    """保存された任意業務契約の版を明記し、公開 task 専用 Schema を補造しない。"""

    contract_version: Literal["projectmind.task-contract-draft/v1"]

    @model_validator(mode="after")
    def verify_original_contract(self) -> Self:
        """元の compiler で深さ・総 field 数・enum 型と重複も検査し、契約を補正しない。"""

        compile_task_contract(self.model_dump(mode="json", exclude_unset=True))
        return self


class FlowDeliverableResponse(_FlowModel):
    """予定交付物だけを表示し、Artifact の保存やダウンロード成功を示さない。"""

    key: Key
    kind: Literal["report", "structured_data", "patch", "change_proposal", "artifact"]
    description: Text


class FlowTaskResponse(_FlowModel):
    """選択した原 Task の field を保持し、欠けた任意項目は補完しない。"""

    key: Key
    capability: Key
    objective: Text
    success_criteria: Annotated[list[FlowNoteResponse], Field(max_length=50)] | None = None
    resource_keys: Annotated[list[Key], Field(max_length=50)] | None = None
    deliverables: Annotated[list[FlowDeliverableResponse], Field(max_length=50)] | None = None
    parameter_contract: FlowContractResponse | None = None
    result_contract: FlowContractResponse | None = None


class FlowResourceResponse(_FlowModel):
    """原資源要求は要求として表示し、候補選択やアクセス済み事実に読み替えない。"""

    key: Key
    kind: ResourceKind
    required: Annotated[bool, Field(strict=True)]
    access: Literal["read", "write"]
    capabilities: Annotated[list[Capability], Field(max_length=20)] | None = None
    accepted_providers: Annotated[list[Key], Field(max_length=20)] | None = None
    selection_guidance: Text | None = None


class FlowGuidanceResponse(_FlowModel):
    """必須・推奨・品質・禁止を混ぜず、Skill 共通の原 guidance を表示する。"""

    required_rules: Annotated[list[FlowNoteResponse], Field(max_length=100)] | None = None
    recommended_steps: Annotated[list[FlowNoteResponse], Field(max_length=100)] | None = None
    quality_criteria: Annotated[list[FlowNoteResponse], Field(max_length=100)] | None = None
    prohibited_actions: Annotated[list[FlowNoteResponse], Field(max_length=100)] | None = None


class FlowInteractionResponse(_FlowModel):
    """予定確認点だけを表示し、OPEN や受理済みなどの持久待办を作らない。"""

    key: Key
    type: Literal["CLARIFICATION", "CHOICE", "REVIEW", "EFFECT_APPROVAL"]
    condition: Text
    prompt: Text | None = None


class FlowEffectResponse(_FlowModel):
    """原効果意図を表示し、承認や外部 apply の成功に変換しない。"""

    key: Key
    mode: Literal["observe", "propose", "apply"]
    resource_key: Key | None = None
    operation: Text
    risk: Literal["low", "medium", "high"]
    approval_mode: Literal["ask"] | None = None


class FlowQuestionResponse(FlowNoteResponse):
    """解釈が残した質問と原 required を、そのまま未知情報として示す。"""

    required: Annotated[bool, Field(strict=True)]


class FlowExecutionPreferencesResponse(_FlowModel):
    """実行推奨は表示だけとし、Run profile や停止証明として利用しない。"""

    recommended_profile: Literal["GUIDED", "SUPERVISED", "DELEGATED"] | None = None
    session_split_hints: Annotated[list[FlowNoteResponse], Field(max_length=100)] | None = None
    stop_conditions: Annotated[list[FlowNoteResponse], Field(max_length=100)] | None = None


class FlowTaskEntryResponse(_FlowModel):
    """選択 Task と原 Blueprint 内の位置を明示する。"""

    scope: Literal["TASK"]
    blueprint_ref: Annotated[str, Field(strict=True, pattern=r"^/tasks/(0|[1-9][0-9]*)$")]
    value: FlowTaskResponse


class FlowResourceEntryResponse(_FlowModel):
    """資源要求の原位置と Task/Skill 適用範囲を保持する。"""

    scope: Literal["TASK", "SKILL"]
    blueprint_ref: Annotated[
        str,
        Field(
            strict=True,
            pattern=r"^/resource_requirements/(0|[1-9][0-9]*)$",
        ),
    ]
    value: FlowResourceResponse


class FlowSharedResponse(_FlowModel):
    """Task 専用と偽らず、全 Skill の共通規則・確認・効果を別区画に残す。"""

    scope: Literal["SKILL"]
    resource_requirements: Annotated[list[FlowResourceEntryResponse], Field(max_length=50)]
    guidance: FlowGuidanceResponse | None
    interaction_points: Annotated[list[FlowInteractionResponse], Field(max_length=50)] | None
    effect_intents: Annotated[list[FlowEffectResponse], Field(max_length=50)] | None
    assumptions: Annotated[list[FlowNoteResponse], Field(max_length=100)] | None
    questions: Annotated[list[FlowQuestionResponse], Field(max_length=50)] | None
    execution_preferences: FlowExecutionPreferencesResponse | None


class FlowPlanResponse(_FlowModel):
    """既存 Blueprint の read-only 構造であり、新たな DAG やノード実行器ではない。"""

    task: FlowTaskEntryResponse
    task_resources: Annotated[list[FlowResourceEntryResponse], Field(max_length=50)]
    shared: FlowSharedResponse


class FlowIdentityResponse(_FlowModel):
    """表示対象を原 Project・SkillVersion・Task・Manifest へ精確に固定する。"""

    project_id: UUID
    skill_id: UUID
    skill_version_id: UUID
    task_id: UUID
    task_key: Key
    skill_key: Key
    version: Annotated[str, Field(strict=True, min_length=1, max_length=32)]
    manifest_checksum: Checksum


class FlowTraceResponse(_FlowModel):
    """検証範囲を持つ原 trace を返し、binary 取得や意味的な正しさは主張しない。"""

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"verification": {"const": "SOURCE_INDEX"}},
                        "required": ["verification"],
                    },
                    "then": {"properties": {"line": {"type": "null"}}},
                }
            ]
        }
    )

    target: Annotated[str, Field(strict=True, max_length=512, pattern=r"^/")]
    path: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
    line: Annotated[int, Field(strict=True, ge=1)] | None
    reason: Text
    verification: Literal["SOURCE_INDEX", "TEXT_SNAPSHOT"]

    @model_validator(mode="after")
    def verify_index_is_file_level(self) -> Self:
        """本文を持たない binary index の確認を、行番号の検証済みに格上げしない。"""

        if self.verification == "SOURCE_INDEX" and self.line is not None:
            raise ValueError("Source index verification cannot assert a line")
        return self


class FlowCandidateResponse(_FlowModel):
    """候補の公開ラベルだけを返し、接続情報・Secret・scope は公開しない。"""

    key: Annotated[str, Field(strict=True, min_length=1)]
    kind: Annotated[str, Field(strict=True, min_length=1)]
    provider: Annotated[str, Field(strict=True, min_length=1)]
    label: Annotated[str, Field(strict=True, min_length=1)]


class FlowRequirementAssessmentResponse(_FlowModel):
    """候補と不足理由を現時点の資源照合として返し、実行済みとは表示しない。"""

    key: Key
    kind: ResourceKind
    required: Annotated[bool, Field(strict=True)]
    access: Literal["read", "write"]
    status: Literal["AVAILABLE", "UNAVAILABLE", "UNSUPPORTED"]
    reason: Annotated[str, Field(strict=True, min_length=1)]
    capabilities: Annotated[list[Capability], Field(max_length=20)]
    selection_guidance: Text | None
    candidates: list[FlowCandidateResponse]


class FlowAssessmentResponse(_FlowModel):
    """既存の四段階 readiness を全 Blueprint の現在状態として保持する。"""

    level: Literal["GUIDANCE_ONLY", "CONFIGURATION_REQUIRED", "RUNNABLE", "ACTIONABLE"]
    requirements: Annotated[list[FlowRequirementAssessmentResponse], Field(max_length=50)]


class FlowReadinessResponse(_FlowModel):
    """未配線なら null のままにし、計画 checksum と現在の候補を分離する。"""

    scope: Literal["SKILL_BLUEPRINT"]
    assessment: FlowAssessmentResponse | None


class TaskFlowPreviewResponse(_FlowModel):
    """新しい read-only preview の完全な公開 shape。Run の frozen plan ではない。"""

    model_config = ConfigDict(json_schema_extra=_preview_schema)

    preview_version: Literal["projectmind.task-flow-preview/v1"]
    identity: FlowIdentityResponse
    status: Literal["AVAILABLE", "NOT_DECLARED"]
    blueprint_checksum: Checksum | None
    preview_checksum: Checksum
    plan: FlowPlanResponse | None
    source_traces: Annotated[list[FlowTraceResponse], Field(max_length=500)]
    readiness: FlowReadinessResponse

    @model_validator(mode="after")
    def verify_state_and_scope(self) -> Self:
        """空の未宣言と壊れた宣言を区別し、Task と Skill の適用範囲を入れ替えない。"""

        if self.status == "NOT_DECLARED":
            if self.plan is not None or self.blueprint_checksum is not None or self.source_traces:
                raise ValueError("Undeclared preview contains a plan")
            if self.readiness.assessment is not None:
                raise ValueError("Undeclared preview contains an assessment")
        else:
            if self.plan is None or self.blueprint_checksum is None or not self.source_traces:
                raise ValueError("Available preview has no plan")
            if self.plan.task.value.key != self.identity.task_key:
                raise ValueError("Preview task does not match its identity")
            if any(item.scope != "TASK" for item in self.plan.task_resources) or any(
                item.scope != "SKILL" for item in self.plan.shared.resource_requirements
            ):
                raise ValueError("Preview resource scope is inconsistent")
            entries = [*self.plan.task_resources, *self.plan.shared.resource_requirements]
            resources = {entry.value.key: entry.value for entry in entries}
            if len(resources) != len(entries) or len(
                {entry.blueprint_ref for entry in entries}
            ) != len(entries):
                raise ValueError("Preview resources contain duplicate identities")
            if [entry.value.key for entry in self.plan.task_resources] != (
                self.plan.task.value.resource_keys or []
            ):
                raise ValueError("Preview task resource declarations are inconsistent")
            assessment = self.readiness.assessment
            if assessment is not None:
                if len(assessment.requirements) != len(resources) or len(
                    {item.key for item in assessment.requirements}
                ) != len(resources):
                    raise ValueError("Readiness does not describe the complete Blueprint")
                for item in assessment.requirements:
                    resource = resources.get(item.key)
                    if resource is None or (
                        item.kind,
                        item.required,
                        item.access,
                        item.capabilities,
                        item.selection_guidance,
                    ) != (
                        resource.kind,
                        resource.required,
                        resource.access,
                        resource.capabilities or [],
                        resource.selection_guidance,
                    ):
                        raise ValueError("Readiness does not match the Blueprint declarations")
        if any(
            value.int == 0
            for value in (
                self.identity.project_id,
                self.identity.skill_id,
                self.identity.skill_version_id,
                self.identity.task_id,
            )
        ):
            raise ValueError("Preview identity must be non-nil")
        if self.identity.task_id != derive_task_id(
            skill_version_id=self.identity.skill_version_id,
            task_key=self.identity.task_key,
        ):
            raise ValueError("Preview task identity is inconsistent")
        return self


@router.get(
    "/projects/{project_id}/skill-versions/{skill_version_id}/tasks/{task_key}/flow-preview",
    response_model=TaskFlowPreviewResponse,
    response_model_exclude_unset=True,
    responses={
        200: {"description": "Verified read-only Task design", "headers": _OPENAPI_HEADERS},
        **_PROBLEMS,
    },
    tags=["tasks", "auth"],
)
async def get_task_flow_preview(
    request: Request,
    response: Response,
    project_id: UUID,
    skill_version_id: UUID,
    actor: ProjectReadActor,
    task_key: Annotated[str, Path(max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")],
) -> TaskFlowPreviewResponse:
    """現在の Project read 認可後に精確版を投影し、GET から業務処理を起動しない。"""

    service: SkillService = request.app.state.skill_service
    try:
        result = await service.get_task_flow_preview(
            organization_id=actor.organization_id,
            project_id=project_id,
            skill_version_id=skill_version_id,
            task_key=task_key,
        )
        projected = _preview_response(
            result,
            project_id=project_id,
            skill_version_id=skill_version_id,
            task_key=task_key,
        )
    except (SkillVersionNotFoundError, PublishedTaskNotFoundError) as error:
        raise ProblemException(
            status=404,
            title="Task Flow preview not found",
            detail="The requested Task Flow preview was not found.",
            code="task_flow_preview_not_found",
            headers=_HEADERS,
        ) from error
    except (TaskFlowPreviewInvalidError, ValidationError) as error:
        raise _invalid_preview_problem() from error
    except (SQLAlchemyError, ConnectionError, TimeoutError) as error:
        raise ProblemException(
            status=503,
            title="Task Flow preview unavailable",
            detail="The saved Task Flow preview is currently unavailable.",
            code="task_flow_preview_unavailable",
            headers=_HEADERS,
        ) from error
    response.headers.update(_HEADERS)
    return projected


def _preview_response(
    result: TaskFlowPreviewResult,
    *,
    project_id: UUID,
    skill_version_id: UUID,
    task_key: str,
) -> TaskFlowPreviewResponse:
    """検証済み domain 値でも URL 帰属と原 checksum を比較してから公開する。"""

    if not isinstance(result, TaskFlowPreviewResult) or not isinstance(
        result.preview,
        TaskFlowPreview,
    ):
        raise TaskFlowPreviewInvalidError()
    try:
        payload = result.preview.to_json()
        response = TaskFlowPreviewResponse.model_validate_json(
            canonical_json(
                {
                    **payload,
                    "readiness": {
                        "scope": "SKILL_BLUEPRINT",
                        "assessment": _assessment(result.readiness),
                    },
                }
            )
        )
    except (ValueError, TypeError, AttributeError, RecursionError) as error:
        raise TaskFlowPreviewInvalidError() from error
    identity = response.identity
    if (identity.project_id, identity.skill_version_id, identity.task_key) != (
        project_id,
        skill_version_id,
        task_key,
    ):
        raise TaskFlowPreviewInvalidError()
    original = {key: value for key, value in payload.items() if key != "preview_checksum"}
    if response.preview_checksum != "sha256:" + sha256_hex(canonical_json(original)):
        raise TaskFlowPreviewInvalidError()
    if response.model_dump(mode="json", exclude_unset=True, exclude={"readiness"}) != payload:
        raise TaskFlowPreviewInvalidError()
    return response


def _assessment(value: TaskReadiness | None) -> dict[str, Any] | None:
    """既存 readiness から明示 field のみを取り出し、private candidate scope を落とす。"""

    if value is None:
        return None
    return {
        "level": value.level.value,
        "requirements": [
            {
                "key": item.key,
                "kind": item.kind,
                "required": item.required,
                "access": item.access,
                "status": item.status.value,
                "reason": item.reason,
                "capabilities": list(item.capabilities),
                "selection_guidance": item.selection_guidance,
                "candidates": [
                    {
                        "key": candidate.key,
                        "kind": candidate.kind,
                        "provider": candidate.provider,
                        "label": candidate.label,
                    }
                    for candidate in item.candidates
                ],
            }
            for item in value.requirements
        ],
    }


def _invalid_preview_problem() -> ProblemException:
    """JSON/SQL/source 本文を露出せず、原設計の検証に失敗した事実だけを返す。"""

    return ProblemException(
        status=409,
        title="Task Flow preview invalid",
        detail="The saved Task Flow preview could not be verified.",
        code="task_flow_preview_invalid",
        headers=_HEADERS,
    )
