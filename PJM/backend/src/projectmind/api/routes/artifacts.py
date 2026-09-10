"""保存済み Artifact だけを現在の Project/Run 認可の下で一覧・添付読取する。"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Path, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from projectmind.api.auth_dependencies import ProjectReadActor
from projectmind.api.problems import ProblemException, problem_openapi_response
from projectmind.api.routes.runs import authorized_run, run_not_found_problem
from projectmind.artifacts.domain import ArtifactIntegrityError, ArtifactMetadata
from projectmind.artifacts.service import ArtifactService
from projectmind.documents.download import attachment_disposition
from projectmind.runs.domain import RunNotFoundError

router = APIRouter()
_READ_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_OPENAPI_HEADERS = {
    key: {"schema": {"type": "string", "const": value}}
    for key, value in _READ_HEADERS.items()
}
_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: problem_openapi_response(
        description,
        headers=_OPENAPI_HEADERS if code in {409, 503} else {
            "Cache-Control": _OPENAPI_HEADERS["Cache-Control"],
        },
    )
    for code, description in (
        (401, "Authentication is required"),
        (403, "Project access is forbidden"),
        (404, "project_not_found, run_not_found, or artifact_not_found"),
        (409, "artifact_content_invalid: saved metadata or content failed verification"),
        (422, "Invalid Project, Run, or Artifact identity"),
        (503, "artifact_storage_unavailable: saved Artifact storage is unavailable"),
    )
}


class ArtifactMetadataResponse(BaseModel):
    """内部 bytes や storage locator を含めない十項目の公開 metadata。"""

    model_config = ConfigDict(extra="forbid")

    artifact_ref: str = Field(max_length=64, pattern=r"^art_[a-zA-Z0-9_-]+$")
    project_id: UUID
    run_id: UUID
    tool_call_id: UUID
    evidence_ref: str = Field(max_length=64, pattern=r"^ev_[a-zA-Z0-9_-]+$")
    path: str = Field(min_length=8, max_length=4096, pattern=r"^output/")
    size_bytes: int = Field(ge=0, le=1_048_576, strict=True)
    mime_type: Literal["text/plain"]
    checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: AwareDatetime = Field(json_schema_extra={
        "pattern": r"(?:Z|[+-]\d{2}:\d{2})$",
    })


@router.get(
    "/projects/{project_id}/runs/{run_id}/artifacts",
    response_model=Annotated[list[ArtifactMetadataResponse], Field(max_length=100)],
    responses={200: {"description": "Saved Artifact metadata", "headers": _OPENAPI_HEADERS},
               **_PROBLEMS},
    tags=["artifacts", "auth"],
)
async def list_artifacts(
    request: Request, response: Response, project_id: UUID, run_id: UUID,
    actor: ProjectReadActor,
) -> list[ArtifactMetadataResponse]:
    """Project 権限と URL の Run 帰属を確かめ、現在の workspace を列挙しない。"""

    service: ArtifactService = request.app.state.artifact_service
    try:
        await _authorize_run(request, actor, project_id, run_id)
        items = await service.list_metadata(project_id=project_id, run_id=run_id)
    except ArtifactIntegrityError as error:
        raise _invalid_artifact_problem() from error
    except (SQLAlchemyError, TimeoutError, ConnectionError) as error:
        raise _storage_unavailable_problem() from error
    response.headers.update(_READ_HEADERS)
    return [_metadata_response(item) for item in items]


@router.get(
    "/projects/{project_id}/runs/{run_id}/artifacts/{artifact_ref}/content",
    response_class=Response,
    responses={
        200: {
            "description": "Complete verified UTF-8 Artifact bytes; no ranges or redirects",
            "content": {"text/plain": {"schema": {
                "type": "string", "format": "binary", "maxLength": 1_048_576,
            }}},
            "headers": {**_OPENAPI_HEADERS,
                        "Content-Disposition": {"schema": {"type": "string"}}},
        },
        **_PROBLEMS,
    },
    tags=["artifacts", "auth"],
)
async def download_artifact(
    request: Request, project_id: UUID, run_id: UUID, actor: ProjectReadActor,
    artifact_ref: Annotated[str, Path(max_length=64, pattern=r"^art_[a-zA-Z0-9_-]+$")],
) -> Response:
    """保存時の同一字節だけを添付とし、HTML や URL の実行へ変換しない。"""

    service: ArtifactService = request.app.state.artifact_service
    try:
        await _authorize_run(request, actor, project_id, run_id)
        content = await service.get_content(
            project_id=project_id, run_id=run_id, artifact_ref=artifact_ref,
        )
    except ArtifactIntegrityError as error:
        raise _invalid_artifact_problem() from error
    except (SQLAlchemyError, TimeoutError, ConnectionError) as error:
        raise _storage_unavailable_problem() from error
    if content is None:
        raise ProblemException(
            status=404, title="Artifact not found", detail="The saved Artifact was not found.",
            code="artifact_not_found", headers=_READ_HEADERS,
        )
    return Response(
        content=content.content, media_type="text/plain",
        headers={**_READ_HEADERS,
                 "Content-Disposition": attachment_disposition(content.metadata.path)},
    )


async def _authorize_run(
    request: Request, actor: ProjectReadActor, project_id: UUID, run_id: UUID,
) -> None:
    """共有 Run 認可に加え、両 Project に権限があっても異なる URL 帰属を許可しない。"""

    run = await authorized_run(request, actor, run_id)
    if run.project_id != project_id:
        raise run_not_found_problem(RunNotFoundError("Run was not found in this Project"))


def _metadata_response(value: ArtifactMetadata) -> ArtifactMetadataResponse:
    """明示 allowlist で投影し、将来追加される内部 field を自動公開しない。"""

    return ArtifactMetadataResponse(
        artifact_ref=value.artifact_ref, project_id=value.project_id, run_id=value.run_id,
        tool_call_id=value.tool_call_id, evidence_ref=value.evidence_ref, path=value.path,
        size_bytes=value.size_bytes, mime_type=cast(Literal["text/plain"], value.mime_type),
        checksum=value.checksum,
        created_at=value.created_at,
    )


def _invalid_artifact_problem() -> ProblemException:
    """破損した内容や DB 詳細を含めず、保存内容を確認できないことだけを返す。"""

    return ProblemException(
        status=409, title="Artifact content invalid",
        detail="The saved Artifact metadata or content could not be verified.",
        code="artifact_content_invalid", headers=_READ_HEADERS,
    )


def _storage_unavailable_problem() -> ProblemException:
    """接続情報を公開せず、別の workspace や URL への fallback も行わない。"""

    return ProblemException(
        status=503, title="Artifact storage unavailable",
        detail="The saved Artifact storage is unavailable.",
        code="artifact_storage_unavailable", headers=_READ_HEADERS,
    )
