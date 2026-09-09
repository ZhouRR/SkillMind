"""Project 作用域の文書 upload/列挙/download/削除 API route を提供する。"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, Response, UploadFile, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from projectmind.api.auth_dependencies import (
    ProjectReadActor,
    ProjectWriteActor,
    authentication_required_problem,
    authorize_project_access,
    csrf_rejected_problem,
    project_archived_problem,
    project_not_found_problem,
    user_access,
)
from projectmind.api.problems import (
    NO_STORE_PROBLEM_HEADERS,
    ProblemException,
    problem_openapi_response,
)
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.documents.domain import (
    DocumentConflictError,
    DocumentContentInvalidError,
    DocumentContentMissingError,
    DocumentInUseError,
    DocumentNotFoundError,
    DocumentReferencesUnavailableError,
    DocumentStorageUnavailableError,
    StoredDocument,
)
from projectmind.documents.download import attachment_disposition, attachment_media_type
from projectmind.documents.service import DocumentService
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from projectmind.storage import UploadRejectedError

router = APIRouter(tags=["auth"])

_ACCESS_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: problem_openapi_response(description, headers=NO_STORE_PROBLEM_HEADERS)
    for code, description in (
        (401, "The original authenticated session must remain valid"),
        (403, "The request origin or original CSRF token was rejected"),
        (404, "The project or exact document is not accessible"),
    )
}


class DocumentResponse(BaseModel):
    """保存済み文書 metadata の公開 response。blob 正文と storage_key は公開しない。"""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    project_id: UUID
    folder: str = Field(max_length=200)
    name: str = Field(min_length=1, max_length=200)
    size: int = Field(ge=0, strict=True)
    mime: str = Field(min_length=1, max_length=128)
    checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    uploaded_by: UUID
    created_at: AwareDatetime


class DocumentListResponse(BaseModel):
    """Project 内で参照可能な文書の一覧。"""

    model_config = ConfigDict(extra="forbid")

    documents: list[DocumentResponse]


@router.post(
    "/projects/{project_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Document with the same name already exists"},
        422: {"description": "Upload rejected by size, type, quota, or content policy"},
    },
    tags=["documents"],
)
async def upload_document(
    request: Request,
    project_id: UUID,
    actor: ProjectWriteActor,
    file: Annotated[UploadFile, File(description="Uploaded document content")],
    folder: Annotated[str, Form()] = "",
) -> DocumentResponse:
    """Project 成員が文書 (binary 可) を upload し、metadata を Project 作用域で保存する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: DocumentService = request.app.state.document_service
    data = await file.read()
    try:
        stored = await service.upload_document(
            project_id=project_id,
            uploaded_by=actor.user_id,
            folder=folder,
            name=file.filename or "",
            data=data,
            content_type=file.content_type or "application/octet-stream",
        )
    except UploadRejectedError as error:
        raise ProblemException(
            status=422,
            title="Document upload rejected",
            detail=str(error),
            code=error.code,
        ) from error
    except DocumentConflictError as error:
        raise ProblemException(
            status=409,
            title="Document already exists",
            detail=str(error),
            code="document_conflict",
        ) from error
    return _document_response(stored)


@router.get(
    "/projects/{project_id}/documents/{document_id}",
    response_model=DocumentResponse,
    responses={200: {"headers": NO_STORE_PROBLEM_HEADERS}, **_ACCESS_PROBLEMS},
    tags=["documents"],
)
async def get_document(
    request: Request, project_id: UUID, document_id: UUID, actor: ProjectReadActor
) -> DocumentResponse:
    """削除結果未知の核対は原 ID の metadata に限り、blob や原 DELETE の成否を推測しない。"""

    service: DocumentService = request.app.state.document_service
    try:
        document = await service.get_document(project_id=project_id, document_id=document_id)
    except DocumentNotFoundError as error:
        raise _document_not_found(error) from error
    return _document_response(document)


@router.get(
    "/projects/{project_id}/documents",
    response_model=DocumentListResponse,
    tags=["documents"],
)
async def list_documents(
    request: Request,
    project_id: UUID,
    actor: ProjectReadActor,
) -> DocumentListResponse:
    """Project 成員に文書 metadata の一覧を返す。"""

    await authorize_project_access(request, actor, project_id)
    service: DocumentService = request.app.state.document_service
    documents = await service.list_documents(project_id=project_id)
    return DocumentListResponse(documents=[_document_response(item) for item in documents])


@router.get(
    "/projects/{project_id}/documents/{document_id}/content",
    response_class=Response,
    responses={
        200: {
            "description": (
                "Verified document bytes with saved MIME, or application/octet-stream for "
                "invalid MIME metadata; always served as an attachment"
            ),
            "content": {
                "*/*": {"schema": {"type": "string", "format": "binary"}}
            },
            "headers": {
                **NO_STORE_PROBLEM_HEADERS,
                "Content-Disposition": {"schema": {"type": "string"}},
                "X-Content-Type-Options": {"schema": {"type": "string", "const": "nosniff"}},
            },
        },
        **_ACCESS_PROBLEMS,
        409: problem_openapi_response(
            "document_content_missing or document_content_invalid",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        503: problem_openapi_response(
            "document_storage_unavailable", headers=NO_STORE_PROBLEM_HEADERS
        ),
        422: problem_openapi_response(
            "Invalid document identity", headers=NO_STORE_PROBLEM_HEADERS
        ),
    },
    tags=["documents"],
)
async def download_document(
    request: Request,
    project_id: UUID,
    document_id: UUID,
    actor: ProjectReadActor,
) -> Response:
    """所有確認と実 byte 検証を経た文書を、安全な添付 header と共に返す。"""

    await authorize_project_access(request, actor, project_id)
    service: DocumentService = request.app.state.document_service
    try:
        document, content = await service.download_document(
            project_id=project_id, document_id=document_id
        )
    except DocumentNotFoundError as error:
        raise _document_not_found(error) from error
    except DocumentContentMissingError as error:
        raise ProblemException(
            status=409,
            title="Document content missing",
            detail="The original document content is missing",
            code="document_content_missing",
        ) from error
    except DocumentContentInvalidError as error:
        raise ProblemException(
            status=409,
            title="Document content invalid",
            detail="Document content does not match its saved metadata",
            code="document_content_invalid",
        ) from error
    except DocumentStorageUnavailableError as error:
        raise ProblemException(
            status=503,
            title="Document storage unavailable",
            detail="Document storage is currently unavailable",
            code="document_storage_unavailable",
        ) from error
    return Response(
        content=content,
        media_type=attachment_media_type(document.mime),
        headers={
            "Content-Disposition": attachment_disposition(document.name),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.delete(
    "/projects/{project_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {
            "description": "Metadata deletion committed; blob cleanup is not certified",
            "headers": NO_STORE_PROBLEM_HEADERS,
        },
        **_ACCESS_PROBLEMS,
        409: problem_openapi_response(
            "project_archived, document_in_use, or document_references_unavailable",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        422: problem_openapi_response(
            "Invalid document identity", headers=NO_STORE_PROBLEM_HEADERS
        ),
    },
    tags=["documents"],
)
async def delete_document(
    request: Request,
    project_id: UUID,
    document_id: UUID,
    actor: ProjectWriteActor,
) -> Response:
    """原会話・Project・参照の門禁を同じ業務 transaction に渡す。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: DocumentService = request.app.state.document_service
    try:
        await service.delete_document(
            project_id=project_id, document_id=document_id, access=user_access(request, actor)
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except (DocumentInUseError, DocumentReferencesUnavailableError) as error:
        in_use = isinstance(error, DocumentInUseError)
        raise ProblemException(
            status=409,
            title="Document deletion blocked",
            detail="Document is referenced"
            if in_use
            else "Document references could not be verified",
            code="document_in_use" if in_use else "document_references_unavailable",
        ) from error
    except DocumentNotFoundError as error:
        raise _document_not_found(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _document_response(document: StoredDocument) -> DocumentResponse:
    """Read model を公開 response へ変換する。"""

    return DocumentResponse(
        document_id=document.document_id,
        project_id=document.project_id,
        folder=document.folder,
        name=document.name,
        size=document.size,
        mime=document.mime,
        checksum=document.checksum,
        uploaded_by=document.uploaded_by,
        created_at=document.created_at,
    )


def _document_not_found(error: Exception) -> ProblemException:
    """文書の不存在/越権を安定した 404 Problem へ変換する。"""

    del error
    return ProblemException(
        status=404,
        title="Document not found",
        detail="Document is not available in this project",
        code="document_not_found",
    )
