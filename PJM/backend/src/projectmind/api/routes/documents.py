"""Project 作用域の文書 upload/列挙/download/削除 API route を提供する。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, Response, UploadFile, status
from pydantic import BaseModel

from projectmind.api.auth_dependencies import (
    ProjectReadActor,
    ProjectWriteActor,
    authorize_project_access,
)
from projectmind.api.problems import ProblemException
from projectmind.documents import (
    DocumentConflictError,
    DocumentNotFoundError,
    DocumentService,
    StoredDocument,
)
from projectmind.storage import UploadRejectedError

router = APIRouter()


class DocumentResponse(BaseModel):
    """保存済み文書 metadata の公開 response。blob 正文と storage_key は公開しない。"""

    document_id: UUID
    project_id: UUID
    folder: str
    name: str
    size: int
    mime: str
    checksum: str
    uploaded_by: UUID
    created_at: datetime


class DocumentListResponse(BaseModel):
    """Project 内で参照可能な文書の一覧。"""

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
    responses={200: {"content": {"application/octet-stream": {}}}},
    tags=["documents"],
)
async def download_document(
    request: Request,
    project_id: UUID,
    document_id: UUID,
    actor: ProjectReadActor,
) -> Response:
    """所有確認済み文書の blob 正文を content-type と共に返す。"""

    await authorize_project_access(request, actor, project_id)
    service: DocumentService = request.app.state.document_service
    try:
        document, content = await service.download_document(
            project_id=project_id, document_id=document_id
        )
    except DocumentNotFoundError as error:
        raise _document_not_found(error) from error
    return Response(
        content=content,
        media_type=document.mime,
        headers={"Content-Disposition": f'attachment; filename="{document.name}"'},
    )


@router.delete(
    "/projects/{project_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "Document not found in project"}},
    tags=["documents"],
)
async def delete_document(
    request: Request,
    project_id: UUID,
    document_id: UUID,
    actor: ProjectWriteActor,
) -> Response:
    """所有確認後に文書 metadata と blob を削除する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: DocumentService = request.app.state.document_service
    try:
        await service.delete_document(project_id=project_id, document_id=document_id)
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

    return ProblemException(
        status=404,
        title="Document not found",
        detail=str(error),
        code="document_not_found",
    )
