"""ADMIN 認証を強制する Skill 解析・保存・version publish API route を提供する。"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from skillmind.api.auth_dependencies import (
    AdminReadActor,
    AdminWriteActor,
    ProjectReadActor,
    administrator_required_problem,
    authentication_required_problem,
    authorize_project_access,
    csrf_rejected_problem,
    project_archived_problem,
    project_not_found_problem,
    user_access,
)
from skillmind.api.problems import ProblemException, problem_openapi_response
from skillmind.api.skill_upload import read_skill_upload
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.runs.domain import RunStatus, TaskLastRun
from skillmind.runs.service import RunService
from skillmind.skills import (
    InlineSkillFile,
    InterpretationLaunch,
    PublishedTaskDescriptor,
    SkillImportError,
    SkillInterpretationNotFoundError,
    SkillInterpretationNotReadyError,
    SkillInterpreterUnavailableError,
    SkillPublishGateError,
    SkillService,
    SkillSourceIntegrityError,
    SkillSourceNotFoundError,
    SkillStorageUnavailableError,
    SkillVersionDeleteBlockedError,
    SkillVersionEnablementConflictError,
    SkillVersionEnablementNotFoundError,
    SkillVersionNotFoundError,
    SkillVersionTransitionError,
    StoredInterpretationExecution,
    StoredProjectSkillVersion,
    StoredSkillPreview,
    StoredSkillVersion,
)
from skillmind.skills.capability_blueprint import resolve_capability_blueprint
from skillmind.skills.domain import SkillPreview
from skillmind.skills.importer import HTTP_SKILL_IMPORT_LIMITS
from skillmind.skills.realtime import (
    INTERPRET_EVENT_NAMES,
    interpret_channel,
    interpret_event_data,
)
from skillmind.skills.resource_binding import TaskReadiness
from skillmind.users.domain import UserAdministrationDeniedError

router = APIRouter()

# Execution key は compute_execution_key が返す固定形式に限る(自由文字列を channel 名へ入れない)。
_EXECUTION_KEY_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

# Interpret job の ARQ timeout(worker 側 1200 秒)より長い SSE 上限。超過は stream 側の打ち切り。
_INTERPRET_STREAM_MAX_SECONDS = 1500.0


class SkillParseFile(BaseModel):
    """Browser から渡す Skill source の単一 text file。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=1_000_000)


class ParseSkillRequest(BaseModel):
    """モデルを呼ばずに Skill source を正規化する request body。"""

    model_config = ConfigDict(extra="forbid")

    files: list[SkillParseFile] = Field(min_length=1, max_length=100)


class ParseSkillResponse(BaseModel):
    """Deterministic parser が返す normalized package、assisted draft と能力蓝图。

    capability_blueprint は Interpreter が生成した蓝图をそのまま返す。manifest が正本であり、
    第二の保存先を作らない。導入直後の決定的 draft はまだ解釈されていないため null になる。
    そこへ機械生成の蓝图を埋めると、Skill が申告していない目標と規則を利用者へ提示することに
    なるため補完しない。Preview は解釈後に能力・目標・資源・規則・交付物・効果を表示する。
    """

    normalized_package: dict[str, Any]
    runtime_manifest_draft: dict[str, Any]
    capability_blueprint: dict[str, Any] | None


class StoredSkillPreviewResponse(BaseModel):
    """保存済み SkillSource と interpretation preview の公開 response。"""

    skill_source_id: UUID
    interpretation_id: UUID
    organization_id: UUID
    name: str
    source_hash: str
    source_type: str
    interpretation_status: str
    compatibility_level: str
    confidence: float
    interpreter_version: str
    created_at: datetime
    preview: ParseSkillResponse


class InterpretationExecutionResponse(BaseModel):
    """Model interpretation 実行結果と親との構造化 diff の公開 response。"""

    interpretation_id: UUID
    skill_source_id: UUID
    organization_id: UUID
    status: str
    origin: str
    model: str | None
    interpreter_version: str
    execution_key: str | None
    error_code: str | None
    compatibility_level: str
    confidence: float
    summary: str
    created_at: datetime
    preview: ParseSkillResponse
    report: dict[str, Any] | None
    reused: bool
    parent_interpretation_id: UUID | None
    adjustment: dict[str, Any] | None
    diff: dict[str, Any]


class InterpretationLaunchResponse(BaseModel):
    """Interpret/adjust 要求の受理結果。

    stored は既存の確定 record(再利用・unsafe 失敗)を同封し、queued は Worker 実行待ちで
    execution_key の SSE stream から進行を観測できることを表す。
    """

    status: str
    execution_key: str
    execution: InterpretationExecutionResponse | None


class AdjustInterpretationRequest(BaseModel):
    """親 interpretation へ適用する append-only な調整指示。"""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=4000)


class ManifestGateFindingResponse(BaseModel):
    """SkillVersion publish gate の公開 finding。"""

    code: str
    severity: str
    message: str
    path: str | None


class SkillVersionResponse(BaseModel):
    """Frozen Manifest と gate report を含む SkillVersion detail。"""

    skill_id: UUID
    skill_version_id: UUID
    skill_source_id: UUID
    interpretation_id: UUID
    organization_id: UUID
    skill_key: str
    name: str
    description: str
    version: str
    status: str
    manifest_checksum: str
    manifest: dict[str, Any]
    gate_passed: bool
    gate_findings: list[ManifestGateFindingResponse]
    interpretation_diff: dict[str, Any]
    created_at: datetime
    published_by: UUID | None
    published_at: datetime | None


class PublishSkillVersionRequest(BaseModel):
    """Publisher が明示受諾する warning code の request。"""

    model_config = ConfigDict(extra="forbid")

    accepted_warnings: list[str] = Field(default_factory=list, max_length=100)


class SkillVersionListResponse(BaseModel):
    """Organization の Skill library に保存された frozen version 一覧。"""

    skill_versions: list[SkillVersionResponse]


class ProjectSkillVersionResponse(BaseModel):
    """Project の明示有効化関係と対象 frozen version の公開投影。"""

    project_id: UUID
    organization_id: UUID
    skill_version: SkillVersionResponse
    enabled_by: UUID
    enabled_at: datetime
    disabled_at: datetime | None


class ProjectSkillVersionListResponse(BaseModel):
    """Project の active または履歴を含む有効化関係一覧。"""

    skill_versions: list[ProjectSkillVersionResponse]


class TaskToolRequirementResponse(BaseModel):
    """Task が要求する Tool capability の公開 response。"""

    capability: str
    required: bool


class ResourceCandidateResponse(BaseModel):
    """資源要求へ束縛できる Project 内候補。Secret や接続情報は含まない。"""

    key: str
    kind: str
    provider: str
    label: str


class RequirementBindingResponse(BaseModel):
    """一つの資源要求の束縛状態と不足理由。

    Workspace が「どの要求が、なぜ、どう解決できるか」を逐項提示できるようにする。単一の
    gate 失敗へ畳むと、利用者は何を設定すればよいか判断できない。
    """

    key: str
    kind: str
    required: bool
    access: str
    status: str
    reason: str
    capabilities: list[str]
    selection_guidance: str | None
    candidates: list[ResourceCandidateResponse]


class TaskReadinessResponse(BaseModel):
    """Task が今この Project で到達できる実行段階と、その逐項根拠。"""

    level: str
    requirements: list[RequirementBindingResponse]


class PublishedTaskResponse(BaseModel):
    """PUBLISHED SkillVersion から投影した実行可能 task descriptor。"""

    skill_id: UUID
    skill_version_id: UUID
    skill_key: str
    skill_name: str
    version: str
    task_key: str
    # Run 側と同じ決定的 ID。画面はこれで task と Run 履歴を突き合わせる。
    task_id: UUID
    capability: str
    title: str
    task_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    input_schema_checksum: str
    output_schema_checksum: str
    task_output_schema: dict[str, Any] | None
    task_output_schema_checksum: str | None
    workflow: str
    view: str
    default_view: str
    compatibility_level: str
    tool_requirements: list[TaskToolRequirementResponse]
    published_at: datetime | None
    # Catalog 未配線の環境では就緒度を判定できない。資源が無いと断定せず null を返す。
    readiness: TaskReadinessResponse | None
    # 一度も実行されていない task は null。「取得できなかった」ではなく「無い」を意味する。
    last_run: TaskLastRunResponse | None


class TaskLastRunResponse(BaseModel):
    """task catalog に添える最新 Run の要約。Run 本体の詳細は Run API が返す。"""

    run_id: UUID
    status: RunStatus
    created_at: datetime
    finished_at: datetime | None
    result_summary: str | None


class TaskCatalogResponse(BaseModel):
    """Project 内で発見可能な実行可能 task の一覧。"""

    tasks: list[PublishedTaskResponse]


@router.post(
    "/skills/parse",
    response_model=ParseSkillResponse,
    responses={400: {"description": "Skill source cannot be parsed deterministically"}},
    tags=["skills"],
)
async def parse_skill(
    request: Request,
    body: ParseSkillRequest,
    actor: AdminWriteActor,
) -> ParseSkillResponse:
    """ADMIN の CSRF 検証後に deterministic Skill preview を返す。"""

    del actor
    service: SkillService = request.app.state.skill_service
    try:
        preview = service.preview_inline(_inline_skill_files(body))
    except SkillImportError as error:
        raise ProblemException(
            status=400,
            title="Skill source rejected",
            detail=error.message,
            code=error.code,
        ) from error
    return _parse_response(preview)


@router.post(
    "/skill-imports",
    response_model=StoredSkillPreviewResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: problem_openapi_response("Skill source cannot be parsed deterministically"),
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        422: problem_openapi_response("Request did not satisfy the API contract"),
        503: problem_openapi_response("Skill source storage is unavailable"),
    },
    tags=["skills"],
)
async def save_skill_import(
    request: Request,
    body: ParseSkillRequest,
    actor: AdminWriteActor,
) -> StoredSkillPreviewResponse:
    """ADMIN の所属 Organization に Skill source を保存する。"""

    service: SkillService = request.app.state.skill_service
    try:
        stored = await service.save_inline(
            access=user_access(request, actor),
            files=_inline_skill_files(body),
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except SkillStorageUnavailableError as error:
        raise _storage_unavailable(error) from error
    except SkillImportError as error:
        raise ProblemException(
            status=400,
            title="Skill source rejected",
            detail=error.message,
            code=error.code,
        ) from error
    return _stored_skill_response(stored)


@router.post(
    "/skill-imports/upload",
    response_model=StoredSkillPreviewResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: problem_openapi_response("Skill source cannot be parsed deterministically"),
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        413: problem_openapi_response("Skill file count, file, header or multipart limit exceeded"),
        422: problem_openapi_response("Invalid or incomplete multipart skill upload"),
        503: problem_openapi_response("Skill source storage is unavailable"),
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": (
                "Only files parts; each filename carries the original relative path. "
                "At most 100 files, 1,000,000 bytes each and 5,000,000 file bytes total. "
                "The whole multipart body adds at most 128 KiB; headers total at most "
                "128 KiB and each part at most 16 KiB."
            ),
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": HTTP_SKILL_IMPORT_LIMITS.max_files,
                                "items": {"type": "string", "format": "binary"},
                            },
                        },
                    },
                },
            },
        },
    },
    tags=["skills"],
)
async def upload_skill_import(
    request: Request,
    actor: AdminWriteActor,
) -> StoredSkillPreviewResponse:
    """ADMIN が multipart で Skill (binary asset 可) を Organization へ保存する。"""

    service: SkillService = request.app.state.skill_service
    access = user_access(request, actor)
    upload_files = await read_skill_upload(request)
    try:
        stored = await service.save_upload(
            access=access,
            files=upload_files,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except SkillStorageUnavailableError as error:
        raise _storage_unavailable(error) from error
    except SkillImportError as error:
        raise ProblemException(
            status=400,
            title="Skill source rejected",
            detail=error.message,
            code=error.code,
        ) from error
    return _stored_skill_response(stored)


@router.get(
    "/skill-interpretations/{interpretation_id}",
    response_model=StoredSkillPreviewResponse,
    responses={404: {"description": "Skill interpretation not found"}},
    tags=["skills"],
)
async def get_skill_interpretation(
    request: Request,
    interpretation_id: UUID,
    actor: AdminReadActor,
) -> StoredSkillPreviewResponse:
    """ADMIN の Organization に属する deterministic interpretation を返す。"""

    service: SkillService = request.app.state.skill_service
    try:
        stored = await service.get_interpretation(
            organization_id=actor.organization_id,
            interpretation_id=interpretation_id,
        )
    except SkillInterpretationNotFoundError as error:
        raise _skill_interpretation_not_found(error) from error
    return _stored_skill_response(stored)


@router.post(
    "/skill-sources/{skill_source_id}/interpret",
    response_model=InterpretationLaunchResponse,
    responses={
        404: {"description": "Skill source not found in organization"},
        409: {"description": "Stored SkillSource integrity check failed"},
        503: {"description": "Skill interpreter is not configured"},
    },
    tags=["skills"],
)
async def interpret_skill_source(
    request: Request,
    skill_source_id: UUID,
    actor: AdminWriteActor,
    force_regenerate: bool = False,
) -> InterpretationLaunchResponse:
    """ADMIN の Organization 内 source の model 解釈を Worker job として受理する。

    Model への egress は Worker だけが持つため、API は再利用/unsafe の同期確定と
    job 投入だけを行い、実行と streaming は Worker + SSE が担う。同一 identity は既定で
    再利用し、force_regenerate の明示時だけ新しい nonce を一回生成する。
    """

    service: SkillService = request.app.state.skill_service
    try:
        launch = await service.begin_interpret(
            organization_id=actor.organization_id,
            skill_source_id=skill_source_id,
            force_regenerate=force_regenerate,
        )
    except SkillSourceNotFoundError as error:
        raise _skill_source_not_found(error) from error
    except SkillInterpreterUnavailableError as error:
        raise _interpreter_unavailable(error) from error
    except SkillStorageUnavailableError as error:
        raise _storage_unavailable(error) from error
    except SkillSourceIntegrityError as error:
        raise _source_integrity_failed(error) from error
    await _enqueue_launch(request, launch)
    return _launch_response(launch)


@router.post(
    "/skill-interpretations/{interpretation_id}/adjust",
    response_model=InterpretationLaunchResponse,
    responses={
        404: {"description": "Skill interpretation not found in organization"},
        409: {"description": "Skill interpretation is not preview-ready"},
        503: {"description": "Skill interpreter is not configured"},
    },
    tags=["skills"],
)
async def adjust_skill_interpretation(
    request: Request,
    interpretation_id: UUID,
    body: AdjustInterpretationRequest,
    actor: AdminWriteActor,
) -> InterpretationLaunchResponse:
    """ADMIN の調整指示を親に折り込み、reinterpretation を Worker job として受理する。"""

    service: SkillService = request.app.state.skill_service
    try:
        launch = await service.begin_adjust(
            organization_id=actor.organization_id,
            interpretation_id=interpretation_id,
            instruction=body.instruction,
            actor_id=actor.user_id,
        )
    except SkillInterpretationNotFoundError as error:
        raise _skill_interpretation_not_found(error) from error
    except SkillInterpretationNotReadyError as error:
        raise _interpretation_not_ready(error) from error
    except SkillInterpreterUnavailableError as error:
        raise _interpreter_unavailable(error) from error
    except SkillStorageUnavailableError as error:
        raise _storage_unavailable(error) from error
    except SkillSourceIntegrityError as error:
        raise _source_integrity_failed(error) from error
    await _enqueue_launch(request, launch)
    return _launch_response(launch)


@router.get(
    "/skill-interpretations/stream/{execution_key}",
    tags=["skills"],
)
async def stream_interpretation_events(
    request: Request,
    execution_key: str,
    actor: AdminReadActor,
) -> StreamingResponse:
    """Interpret job の進行 event(prompt/delta/終端)を SSE で配信する。

    確定済み execution は接続直後に終端 event を即時回放し、未確定は Redis Pub/Sub を
    転送する。正本は skill_interpretations の永続 record で、stream は表示専用。
    """

    if not _EXECUTION_KEY_PATTERN.fullmatch(execution_key):
        raise ProblemException(
            status=422,
            title="Invalid execution key",
            detail="The execution key does not match the expected format.",
            code="invalid_execution_key",
        )
    service: SkillService = request.app.state.skill_service
    redis: Redis = request.app.state.redis
    heartbeat: float = request.app.state.settings.sse_heartbeat_seconds

    async def stream() -> Any:
        """終端の即時回放 → Pub/Sub 転送 → 上限打ち切りの順で配信する。"""

        pubsub = redis.pubsub()
        try:
            try:
                # DB 確認より先に subscribe し、確定直前の event 取りこぼし窓を閉じる。
                await pubsub.subscribe(interpret_channel(execution_key))
            except RedisError:
                yield _interpret_sse_message(_stream_failure(execution_key, "stream_unavailable"))
                return
            terminal = await service.find_terminal_execution(
                organization_id=actor.organization_id,
                execution_key=execution_key,
            )
            if terminal is not None:
                interpretation_id, status_value, error_code = terminal
                name = (
                    "interpret.completed" if status_value == "PREVIEW_READY" else "interpret.failed"
                )
                yield _interpret_sse_message(
                    interpret_event_data(
                        event=name,
                        execution_key=execution_key,
                        data={
                            "interpretation_id": str(interpretation_id),
                            "status": status_value,
                            "error_code": error_code,
                            "reused": True,
                        },
                    )
                )
                return
            deadline = monotonic() + _INTERPRET_STREAM_MAX_SECONDS
            last_activity = monotonic()
            while monotonic() < deadline:
                if await request.is_disconnected():
                    return
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except RedisError:
                    yield _interpret_sse_message(
                        _stream_failure(execution_key, "stream_unavailable")
                    )
                    return
                if message is None:
                    if monotonic() - last_activity >= heartbeat:
                        last_activity = monotonic()
                        yield f": heartbeat {datetime.now(UTC).isoformat()}\n\n"
                    continue
                value = _parse_interpret_event(message.get("data"), execution_key=execution_key)
                if value is None:
                    continue
                last_activity = monotonic()
                yield _interpret_sse_message(value)
                if value["event"] in {"interpret.completed", "interpret.failed"}:
                    return
            # Worker の job timeout を超えても終端が来ない場合は client 側で再試行させる。
            yield _interpret_sse_message(_stream_failure(execution_key, "stream_timeout"))
        finally:
            close_pubsub = cast(Callable[[], Awaitable[None]], pubsub.aclose)
            await close_pubsub()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _enqueue_launch(request: Request, launch: InterpretationLaunch) -> None:
    """Queued な受理結果を ARQ job として投入する。

    job_id は execution key で固定し、多重 click を queue 側でも重複排除する。既に同じ
    job が存在する場合の enqueue は None を返すが、その実行が終端 event を配信する。
    """

    if launch.status != "queued":
        return
    pool = cast(ArqRedis, request.app.state.arq_pool)
    await pool.enqueue_job(
        launch.job_name,
        launch.job_kwargs,
        _job_id=f"interpret:{launch.execution_key}",
        _queue_name=request.app.state.settings.queue_name,
    )


def _launch_response(launch: InterpretationLaunch) -> InterpretationLaunchResponse:
    """受理結果を公開 response へ変換する。"""

    return InterpretationLaunchResponse(
        status=launch.status,
        execution_key=launch.execution_key,
        execution=_execution_response(launch.stored) if launch.stored is not None else None,
    )


def _interpret_sse_message(value: dict[str, Any]) -> str:
    """進行 event を named SSE frame へ変換する。"""

    return (
        f"event: {value['event']}\n"
        f"data: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _parse_interpret_event(raw: Any, *, execution_key: str) -> dict[str, Any] | None:
    """Pub/Sub message を同じ execution の既知 event だけへ制限する。"""

    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(value, dict)
        or value.get("execution_key") != execution_key
        or value.get("event") not in INTERPRET_EVENT_NAMES
        or not isinstance(value.get("data"), dict)
    ):
        return None
    return value


def _stream_failure(execution_key: str, code: str) -> dict[str, Any]:
    """Stream 側の打ち切りを終端 failed event として表現する(永続 record は変更しない)。"""

    return interpret_event_data(
        event="interpret.failed",
        execution_key=execution_key,
        data={"interpretation_id": None, "status": None, "error_code": code, "reused": False},
    )


@router.get(
    "/skill-interpretations/{interpretation_id}/execution",
    response_model=InterpretationExecutionResponse,
    responses={404: {"description": "Skill interpretation not found in organization"}},
    tags=["skills"],
)
async def get_skill_interpretation_execution(
    request: Request,
    interpretation_id: UUID,
    actor: AdminReadActor,
) -> InterpretationExecutionResponse:
    """ADMIN の Organization 内 model interpretation 実行 detail を返す。"""

    service: SkillService = request.app.state.skill_service
    try:
        stored = await service.get_interpretation_execution(
            organization_id=actor.organization_id,
            interpretation_id=interpretation_id,
        )
    except SkillInterpretationNotFoundError as error:
        raise _skill_interpretation_not_found(error) from error
    return _execution_response(stored)


@router.post(
    "/skill-interpretations/{interpretation_id}/draft",
    response_model=SkillVersionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        404: {"description": "Skill interpretation not found in organization"},
        409: problem_openapi_response(
            "Skill interpretation is not ready to create a draft",
        ),
    },
    tags=["skills"],
)
async def create_skill_version_draft(
    request: Request,
    interpretation_id: UUID,
    actor: AdminWriteActor,
) -> SkillVersionResponse:
    """ADMIN の Organization 内 interpretation から frozen DRAFT を作成する。"""

    service: SkillService = request.app.state.skill_service
    try:
        stored = await service.create_version_draft(
            access=user_access(request, actor),
            interpretation_id=interpretation_id,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except SkillInterpretationNotFoundError as error:
        raise _skill_interpretation_not_found(error) from error
    except SkillInterpretationNotReadyError as error:
        raise _interpretation_not_ready(error) from error
    return _skill_version_response(stored)


@router.get(
    "/skill-versions",
    response_model=SkillVersionListResponse,
    tags=["skills"],
)
async def list_skill_versions(
    request: Request,
    actor: AdminReadActor,
) -> SkillVersionListResponse:
    """ADMIN に所属 Organization の Skill library 全 version を返す。"""

    service: SkillService = request.app.state.skill_service
    stored = await service.list_skill_versions(organization_id=actor.organization_id)
    return SkillVersionListResponse(
        skill_versions=[_skill_version_response(version) for version in stored]
    )


@router.get(
    "/skill-versions/{skill_version_id}",
    response_model=SkillVersionResponse,
    responses={404: {"description": "Skill version not found in organization"}},
    tags=["skills"],
)
async def get_skill_version(
    request: Request,
    skill_version_id: UUID,
    actor: AdminReadActor,
) -> SkillVersionResponse:
    """ADMIN の Organization に限定して SkillVersion を返す。"""

    service: SkillService = request.app.state.skill_service
    try:
        stored = await service.get_skill_version(
            organization_id=actor.organization_id,
            skill_version_id=skill_version_id,
        )
    except SkillVersionNotFoundError as error:
        raise _skill_version_not_found(error) from error
    return _skill_version_response(stored)


@router.post(
    "/skill-versions/{skill_version_id}/publish",
    response_model=SkillVersionResponse,
    responses={
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        404: {"description": "Skill version not found in organization"},
        409: {"description": "Skill version publish gate did not pass"},
    },
    tags=["skills"],
)
async def publish_skill_version(
    request: Request,
    skill_version_id: UUID,
    body: PublishSkillVersionRequest,
    actor: AdminWriteActor,
) -> SkillVersionResponse:
    """ADMIN identity を publisher audit field に保存して SkillVersion を公開する。"""

    service: SkillService = request.app.state.skill_service
    try:
        stored = await service.publish_skill_version(
            access=user_access(request, actor),
            skill_version_id=skill_version_id,
            accepted_warnings=frozenset(body.accepted_warnings),
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except SkillVersionNotFoundError as error:
        raise _skill_version_not_found(error) from error
    except SkillPublishGateError as error:
        raise ProblemException(
            status=409,
            title="Skill publish gate failed",
            detail=str(error),
            code="skill_publish_gate_failed",
        ) from error
    except SkillVersionTransitionError as error:
        raise ProblemException(
            status=409,
            title="Skill version transition rejected",
            detail=str(error),
            code="skill_version_transition_rejected",
        ) from error
    return _skill_version_response(stored)


@router.post(
    "/skill-versions/{skill_version_id}/deprecate",
    response_model=SkillVersionResponse,
    responses={
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        404: problem_openapi_response("Skill version not found in organization"),
        409: problem_openapi_response("Skill version cannot enter DEPRECATED"),
    },
    tags=["skills"],
)
async def deprecate_skill_version(
    request: Request,
    skill_version_id: UUID,
    actor: AdminWriteActor,
) -> SkillVersionResponse:
    """ADMIN が PUBLISHED 版を廃止し、新規 discovery と Run 作成を閉じる。"""

    service: SkillService = request.app.state.skill_service
    try:
        stored = await service.deprecate_skill_version(
            access=user_access(request, actor),
            skill_version_id=skill_version_id,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except SkillVersionNotFoundError as error:
        raise _skill_version_not_found(error) from error
    except SkillVersionTransitionError as error:
        raise ProblemException(
            status=409,
            title="Skill version transition rejected",
            detail=str(error),
            code="skill_version_transition_rejected",
        ) from error
    return _skill_version_response(stored)


@router.delete(
    "/skill-versions/{skill_version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        404: problem_openapi_response("Skill version not found in organization"),
        409: problem_openapi_response("Skill version is still referenced or not deprecated"),
    },
    tags=["skills"],
)
async def delete_skill_version(
    request: Request,
    skill_version_id: UUID,
    actor: AdminWriteActor,
) -> Response:
    """ADMIN が監査参照のない DEPRECATED 版を library から取り除く。

    廃止しただけでは一覧から消えないため、二度と使わない版が増え続ける。監査の正本を守る
    ため、Run snapshot・ChangeProposal・Composition・Schedule/Occurrence・FrontendModule
    から参照されている版は、参照の状態に関係なく 409 で拒否する。
    """

    service: SkillService = request.app.state.skill_service
    try:
        await service.delete_skill_version(
            access=user_access(request, actor),
            skill_version_id=skill_version_id,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except SkillVersionNotFoundError as error:
        raise _skill_version_not_found(error) from error
    except SkillVersionDeleteBlockedError as error:
        raise ProblemException(
            status=409,
            title="Skill version delete rejected",
            detail=str(error),
            code="skill_version_delete_blocked",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/projects/{project_id}/skill-versions",
    response_model=ProjectSkillVersionListResponse,
    responses={404: {"description": "Project not found"}},
    tags=["skills"],
)
async def list_project_skill_versions(
    request: Request,
    project_id: UUID,
    actor: AdminReadActor,
    include_disabled: bool = False,
) -> ProjectSkillVersionListResponse:
    """ADMIN に Project の active または全 version 有効化関係を返す。"""

    await authorize_project_access(request, actor, project_id)
    service: SkillService = request.app.state.skill_service
    try:
        stored = await service.list_project_skill_versions(
            organization_id=actor.organization_id,
            project_id=project_id,
            include_disabled=include_disabled,
        )
    except SkillVersionEnablementNotFoundError as error:
        # Project authorization と repository scope のいずれも資源の存在を公開しない同じ 404。
        raise _project_skill_version_not_found(error) from error
    return ProjectSkillVersionListResponse(
        skill_versions=[_project_skill_version_response(item) for item in stored]
    )


@router.put(
    "/projects/{project_id}/skill-versions/{skill_version_id}",
    response_model=ProjectSkillVersionResponse,
    responses={
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        404: problem_openapi_response("Project or Skill version not found"),
        409: problem_openapi_response("Skill version cannot be enabled or Project is archived"),
    },
    tags=["skills"],
)
async def enable_project_skill_version(
    request: Request,
    project_id: UUID,
    skill_version_id: UUID,
    actor: AdminWriteActor,
) -> ProjectSkillVersionResponse:
    """ADMIN が同じ Organization の PUBLISHED 精確版を Project へ有効化する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: SkillService = request.app.state.skill_service
    try:
        stored = await service.enable_project_skill_version(
            access=user_access(request, actor),
            project_id=project_id,
            skill_version_id=skill_version_id,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except (SkillVersionNotFoundError, SkillVersionEnablementNotFoundError) as error:
        raise _project_skill_version_not_found(error) from error
    except SkillVersionEnablementConflictError as error:
        raise _project_skill_version_conflict(error) from error
    return _project_skill_version_response(stored)


@router.delete(
    "/projects/{project_id}/skill-versions/{skill_version_id}",
    response_model=ProjectSkillVersionResponse,
    responses={
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        404: problem_openapi_response("Project Skill version enablement not found"),
        409: problem_openapi_response("Project is archived or Skill version binding is invalid"),
    },
    tags=["skills"],
)
async def disable_project_skill_version(
    request: Request,
    project_id: UUID,
    skill_version_id: UUID,
    actor: AdminWriteActor,
) -> ProjectSkillVersionResponse:
    """ADMIN が有効化監査行を残したまま Project から精確版を停用する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: SkillService = request.app.state.skill_service
    try:
        stored = await service.disable_project_skill_version(
            access=user_access(request, actor),
            project_id=project_id,
            skill_version_id=skill_version_id,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except (SkillVersionNotFoundError, SkillVersionEnablementNotFoundError) as error:
        raise _project_skill_version_not_found(error) from error
    except SkillVersionEnablementConflictError as error:
        raise _project_skill_version_conflict(error) from error
    return _project_skill_version_response(stored)


@router.get(
    "/projects/{project_id}/tasks",
    response_model=TaskCatalogResponse,
    responses={404: {"description": "Project not found"}},
    tags=["tasks"],
)
async def list_project_tasks(
    request: Request,
    project_id: UUID,
    actor: ProjectReadActor,
) -> TaskCatalogResponse:
    """Project member に PUBLISHED SkillVersion 由来の実行可能 task catalog を返す。

    「上次执行」はここで Run 側の投影と合流させる。画面が Run 履歴の先頭 N 件を引いて
    突き合わせる形だと、N 件より古い task が「未実行」と表示され、欠落ではなく誤った値が並ぶ。
    合流を route で行うのは、task catalog(Skill 由来)と Run 履歴(実行由来)の read model を
    互いに依存させないため。
    """

    # ProjectReadActor が Project の読み取り権限と 404 畳み込みを強制する。
    del actor
    service: SkillService = request.app.state.skill_service
    run_service: RunService = request.app.state.run_service
    tasks = await service.list_published_tasks(project_id=project_id)
    last_runs = await run_service.latest_run_by_task(project_id=project_id)
    return _task_catalog_response(tasks, last_runs)


def _inline_skill_files(body: ParseSkillRequest) -> tuple[InlineSkillFile, ...]:
    """HTTP request file を Skill domain value へ変換する。"""

    return tuple(InlineSkillFile(path=file.path, content=file.content) for file in body.files)


def _stored_skill_response(stored: StoredSkillPreview) -> StoredSkillPreviewResponse:
    """Skill read model を公開 API response へ変換する。"""

    return StoredSkillPreviewResponse(
        skill_source_id=stored.skill_source_id,
        interpretation_id=stored.interpretation_id,
        organization_id=stored.organization_id,
        name=stored.name,
        source_hash=stored.source_hash,
        source_type=stored.source_type,
        interpretation_status=stored.interpretation_status.value,
        compatibility_level=stored.compatibility_level,
        confidence=stored.confidence,
        interpreter_version=stored.interpreter_version,
        created_at=stored.created_at,
        preview=_parse_response(stored.preview),
    )


def _parse_response(preview: SkillPreview) -> ParseSkillResponse:
    """Preview read model を公開 response へ変換し、能力蓝图を解決する。"""

    return ParseSkillResponse(
        normalized_package=preview.normalized_package,
        runtime_manifest_draft=preview.runtime_manifest_draft,
        capability_blueprint=resolve_capability_blueprint(preview.runtime_manifest_draft),
    )


def _skill_version_response(stored: StoredSkillVersion) -> SkillVersionResponse:
    """SkillVersion read model を公開 field の許可リストへ変換する。"""

    return SkillVersionResponse(
        skill_id=stored.skill_id,
        skill_version_id=stored.skill_version_id,
        skill_source_id=stored.skill_source_id,
        interpretation_id=stored.interpretation_id,
        organization_id=stored.organization_id,
        skill_key=stored.skill_key,
        name=stored.name,
        description=stored.description,
        version=stored.version,
        status=stored.status.value,
        manifest_checksum=stored.manifest_checksum,
        manifest=stored.manifest,
        gate_passed=stored.gate_passed,
        gate_findings=[
            ManifestGateFindingResponse(
                code=item.code,
                severity=item.severity,
                message=item.message,
                path=item.path,
            )
            for item in stored.gate_findings
        ],
        interpretation_diff=stored.interpretation_diff,
        created_at=stored.created_at,
        published_by=stored.published_by,
        published_at=stored.published_at,
    )


def _execution_response(
    stored: StoredInterpretationExecution,
) -> InterpretationExecutionResponse:
    """Interpretation 実行 read model を公開 response の許可リストへ変換する。"""

    return InterpretationExecutionResponse(
        interpretation_id=stored.interpretation_id,
        skill_source_id=stored.skill_source_id,
        organization_id=stored.organization_id,
        status=stored.status.value,
        origin=stored.origin,
        model=stored.model,
        interpreter_version=stored.interpreter_version,
        execution_key=stored.execution_key,
        error_code=stored.error_code,
        compatibility_level=stored.compatibility_level,
        confidence=stored.confidence,
        summary=stored.summary,
        created_at=stored.created_at,
        preview=_parse_response(stored.preview),
        report=stored.report,
        reused=stored.reused,
        parent_interpretation_id=stored.parent_interpretation_id,
        adjustment=stored.adjustment,
        diff=stored.diff,
    )


def _project_skill_version_response(
    stored: StoredProjectSkillVersion,
) -> ProjectSkillVersionResponse:
    """Project 有効化 read model を公開 field の許可リストへ変換する。"""

    return ProjectSkillVersionResponse(
        project_id=stored.project_id,
        organization_id=stored.organization_id,
        skill_version=_skill_version_response(stored.skill_version),
        enabled_by=stored.enabled_by,
        enabled_at=stored.enabled_at,
        disabled_at=stored.disabled_at,
    )


def _task_last_run_response(last_run: TaskLastRun | None) -> TaskLastRunResponse | None:
    """最新 Run の要約を公開 field だけへ写す。未実行は null。"""

    if last_run is None:
        return None
    return TaskLastRunResponse(
        run_id=last_run.run_id,
        status=last_run.status,
        created_at=last_run.created_at,
        finished_at=last_run.finished_at,
        result_summary=last_run.result_summary,
    )


def _task_catalog_response(
    tasks: tuple[PublishedTaskDescriptor, ...],
    last_runs: Mapping[UUID, TaskLastRun],
) -> TaskCatalogResponse:
    """Task descriptor read model を公開 field の許可リストへ変換する。"""

    return TaskCatalogResponse(
        tasks=[
            PublishedTaskResponse(
                skill_id=task.skill_id,
                skill_version_id=task.skill_version_id,
                skill_key=task.skill_key,
                skill_name=task.skill_name,
                version=task.version,
                task_key=task.task_key,
                task_id=task.task_id,
                last_run=_task_last_run_response(last_runs.get(task.task_id)),
                capability=task.capability,
                title=task.title,
                task_type=task.task_type,
                input_schema=task.input_schema,
                output_schema=task.output_schema,
                input_schema_checksum=task.input_schema_checksum,
                output_schema_checksum=task.output_schema_checksum,
                task_output_schema=task.task_output_schema,
                task_output_schema_checksum=task.task_output_schema_checksum,
                workflow=task.workflow,
                view=task.view,
                default_view=task.default_view,
                compatibility_level=task.compatibility_level,
                tool_requirements=[
                    TaskToolRequirementResponse(
                        capability=item.capability,
                        required=item.required,
                    )
                    for item in task.tool_requirements
                ],
                published_at=task.published_at,
                readiness=_readiness_response(task.readiness),
            )
            for task in tasks
        ],
    )


def _readiness_response(readiness: TaskReadiness | None) -> TaskReadinessResponse | None:
    """就緒度 read model を公開 response へ変換する。未判定は null のまま返す。"""

    if readiness is None:
        return None
    return TaskReadinessResponse(
        level=readiness.level.value,
        requirements=[
            RequirementBindingResponse(
                key=item.key,
                kind=item.kind,
                required=item.required,
                access=item.access,
                status=item.status.value,
                reason=item.reason,
                capabilities=list(item.capabilities),
                selection_guidance=item.selection_guidance,
                candidates=[
                    ResourceCandidateResponse(
                        key=candidate.key,
                        kind=candidate.kind,
                        provider=candidate.provider,
                        label=candidate.label,
                    )
                    for candidate in item.candidates
                ],
            )
            for item in readiness.requirements
        ],
    )


def _skill_source_not_found(error: Exception) -> ProblemException:
    """SkillSource の不存在を安定した 404 Problem へ変換する。"""

    return ProblemException(
        status=404,
        title="Skill source not found",
        detail=str(error),
        code="skill_source_not_found",
    )


def _interpreter_unavailable(error: Exception) -> ProblemException:
    """Interpreter 未配線を安定した 503 Problem へ変換する。"""

    return ProblemException(
        status=503,
        title="Skill interpreter unavailable",
        detail=str(error),
        code="skill_interpreter_unavailable",
    )


def _storage_unavailable(error: Exception) -> ProblemException:
    """既知の storage 拒否を静的 503 とし、source/資格/接続情報を公開しない。"""

    del error
    return ProblemException(
        status=503,
        title="Skill storage unavailable",
        detail="The Skill source storage is unavailable.",
        code="skill_storage_unavailable",
    )


def _source_integrity_failed(error: Exception) -> ProblemException:
    """保存済み source の再構築失敗を再実行不能な 409 Problem へ変換する。"""

    return ProblemException(
        status=409,
        title="Skill source integrity check failed",
        detail=str(error),
        code="skill_source_integrity_failed",
    )


def _interpretation_not_ready(error: Exception) -> ProblemException:
    """PREVIEW_READY でない interpretation への操作を 409 Problem へ変換する。"""

    del error
    return ProblemException(
        status=409,
        title="Skill interpretation not ready",
        detail="Skill interpretation is not ready for this operation",
        code="skill_interpretation_not_ready",
    )


def _skill_interpretation_not_found(error: Exception) -> ProblemException:
    """Interpretation の不存在を安定した 404 Problem へ変換する。"""

    return ProblemException(
        status=404,
        title="Skill interpretation not found",
        detail=str(error),
        code="skill_interpretation_not_found",
    )


def _skill_version_not_found(error: Exception) -> ProblemException:
    """SkillVersion の不存在を安定した 404 Problem へ変換する。"""

    return ProblemException(
        status=404,
        title="Skill version not found",
        detail=str(error),
        code="skill_version_not_found",
    )


def _project_skill_version_not_found(error: Exception) -> ProblemException:
    """Project/version の不存在、越権、未有効化を同じ 404 Problem へ畳む。"""

    return ProblemException(
        status=404,
        title="Project Skill version not found",
        detail=str(error),
        code="project_skill_version_not_found",
    )


def _project_skill_version_conflict(error: Exception) -> ProblemException:
    """未発行版または停用済み監査行の再有効化を安定した 409 へ変換する。"""

    return ProblemException(
        status=409,
        title="Project Skill version rejected",
        detail=str(error),
        code="project_skill_version_rejected",
    )
