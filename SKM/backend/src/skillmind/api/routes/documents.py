"""Project 作用域の文書 upload/列挙/download/削除 API route を提供する。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Request, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.exc import SQLAlchemyError

from skillmind.api.auth_dependencies import (
    ProjectReadActor,
    ProjectWriteActor,
    authentication_required_problem,
    authorize_project_access,
    csrf_rejected_problem,
    project_archived_problem,
    project_not_found_problem,
    user_access,
)
from skillmind.api.document_upload import read_document_upload
from skillmind.api.problems import (
    NO_STORE_PROBLEM_HEADERS,
    ProblemException,
    problem_openapi_response,
)
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.documents.domain import (
    DocumentConflictError,
    DocumentContentInvalidError,
    DocumentContentMissingError,
    DocumentInUseError,
    DocumentNotFoundError,
    DocumentReferencesUnavailableError,
    DocumentStorageUnavailableError,
    DocumentUploadAlreadyPublishedError,
    DocumentUploadClosedError,
    DocumentUploadClosureNotFoundError,
    DocumentUploadInvalidError,
    DocumentUploadKeyConflictError,
    DocumentUploadNotFoundError,
    DocumentUploadPendingError,
    StoredDocument,
    StoredDocumentUpload,
    StoredDocumentUploadClosure,
)
from skillmind.documents.download import attachment_disposition, attachment_media_type
from skillmind.documents.service import DocumentService
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.storage import FileStorageError, UploadRejectedError

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


class DocumentUploadResponse(BaseModel):
    """原 upload の保存事実だけを公開し、現在の文書一覧や清理完了と混同しない。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "if": {"properties": {"state": {"const": "PENDING"}}},
            "then": {"properties": {"document": {"type": "null"}}},
            "else": {"properties": {"document": {"type": "object"}}},
        },
    )

    upload_key: UUID = Field(
        json_schema_extra={
            "not": {"const": "00000000-0000-0000-0000-000000000000"},
        }
    )
    project_id: UUID
    state: Literal["PENDING", "PUBLISHED"]
    created_at: AwareDatetime = Field(
        json_schema_extra={
            "pattern": r"(?:[zZ]|[+-][0-9]{2}:[0-9]{2})$",
        }
    )
    document: DocumentResponse | None = Field(
        json_schema_extra={
            "properties": {"created_at": {"pattern": r"(?:[zZ]|[+-][0-9]{2}:[0-9]{2})$"}},
        }
    )


class DocumentUploadClosureRequest(BaseModel):
    """原 key の公開停止だけを明示確認し、削除や quota 返却は要求しない。"""

    model_config = ConfigDict(extra="forbid")

    confirmation: Literal["STOP_PUBLICATION"]


class DocumentUploadClosureResponse(BaseModel):
    """独立の閉鎖回执。元の upload 応答の enum や field を変更しない。"""

    model_config = ConfigDict(extra="forbid")

    upload_key: UUID = Field(
        json_schema_extra={
            "not": {"const": "00000000-0000-0000-0000-000000000000"},
        }
    )
    project_id: UUID = Field(
        json_schema_extra={
            "not": {"const": "00000000-0000-0000-0000-000000000000"},
        }
    )
    document_id: UUID = Field(
        json_schema_extra={
            "not": {"const": "00000000-0000-0000-0000-000000000000"},
        }
    )
    closed_at: AwareDatetime = Field(
        json_schema_extra={
            "pattern": r"(?:[zZ]|[+-][0-9]{2}:[0-9]{2})$",
        }
    )
    publication_state: Literal["CLOSED"]

    @field_validator("upload_key", "project_id", "document_id")
    @classmethod
    def non_nil_identity(cls, value: UUID) -> UUID:
        """内部 DTO の壊れた identity も成功回执として公開しない。"""

        if value.int == 0:
            raise ValueError("A non-zero identity is required")
        return value


# path は route で共通 Problem に変換するため文字列で受け、公開 schema は UUID とする。
UploadClosureKey = Annotated[
    str,
    Path(
        json_schema_extra={
            "format": "uuid",
            "minLength": 36,
            "maxLength": 36,
            "not": {"const": "00000000-0000-0000-0000-000000000000"},
        }
    ),
]


@router.post(
    "/projects/{project_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"headers": NO_STORE_PROBLEM_HEADERS},
        **_ACCESS_PROBLEMS,
        409: problem_openapi_response(
            "document_conflict, project_archived, document_upload_key_conflict, or "
            "document_upload_pending (the original upload outcome remains unknown), or "
            "document_upload_closed (publication is closed, not proof of storage cleanup)",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        413: problem_openapi_response(
            "document_upload_too_large: actual file or multipart bytes exceed the limit",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        422: problem_openapi_response(
            "invalid_document_upload_key, invalid multipart, path, type, quota, or content policy",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        503: problem_openapi_response(
            "document_storage_unavailable or document_upload_unavailable; "
            "upload outcome may be unknown",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
    },
    # File/Form parameter は dependency 前に本文を読むため宣言しない。公開形状は維持する。
    openapi_extra={
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "description": (
                    "One original non-zero UUID per upload; preserve it for result confirmation."
                ),
                "schema": {
                    "type": "string",
                    "format": "uuid",
                    "minLength": 36,
                    "maxLength": 36,
                    "not": {"const": "00000000-0000-0000-0000-000000000000"},
                },
            }
        ],
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "additionalProperties": False,
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "folder": {"type": "string", "default": "", "maxLength": 200},
                        },
                    }
                }
            },
            "description": (
                "Exactly one file and optional UTF-8 folder; authentication precedes body reading. "
                "Actual file bytes are bounded by the configured document limit; total multipart "
                "bytes may exceed that limit by at most 16 KiB. Duplicate fields are rejected."
            ),
        },
    },
    tags=["documents"],
)
async def upload_document(
    request: Request,
    project_id: UUID,
    actor: ProjectWriteActor,
) -> DocumentResponse:
    """入口資格の確認後だけ有界 multipart を読み、原会話を保存 transaction へ渡す。"""

    service: DocumentService = request.app.state.document_service
    access = user_access(request, actor)
    upload_key = _upload_key_header(request)
    upload = await read_document_upload(request, max_bytes=service.max_upload_bytes)
    try:
        stored = await service.upload_document(
            project_id=project_id,
            upload_key=upload_key,
            access=access,
            folder=upload.folder,
            name=upload.name,
            data=upload.data,
            content_type=upload.content_type,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
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
            detail="Document with the same path already exists",
            code="document_conflict",
        ) from error
    except DocumentUploadKeyConflictError as error:
        raise ProblemException(
            status=409,
            title="Document upload key conflict",
            detail="The original upload key belongs to different upload content.",
            code="document_upload_key_conflict",
        ) from error
    except DocumentUploadPendingError as error:
        raise ProblemException(
            status=409,
            title="Document upload pending",
            detail="The original upload outcome remains pending; no new upload was started.",
            code="document_upload_pending",
        ) from error
    except DocumentUploadClosedError as error:
        raise ProblemException(
            status=409,
            title="Document upload closed",
            detail="Publication of the original upload was explicitly closed.",
            code="document_upload_closed",
        ) from error
    except DocumentUploadInvalidError as error:
        raise _document_upload_unavailable() from error
    except (DocumentStorageUnavailableError, FileStorageError) as error:
        raise _document_storage_unavailable() from error
    return _document_response(stored)


@router.get(
    "/projects/{project_id}/document-uploads/{upload_key}",
    response_model=DocumentUploadResponse,
    responses={
        200: {
            "description": (
                "Original upload receipt: PENDING has document=null; PUBLISHED returns the "
                "original metadata even if the document was subsequently deleted"
            ),
            "headers": NO_STORE_PROBLEM_HEADERS,
        },
        **_ACCESS_PROBLEMS,
        404: problem_openapi_response(
            "project_not_found or document_upload_not_found; absence does not prove an "
            "earlier POST cannot still be admitted",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        422: problem_openapi_response(
            "Invalid original upload identity",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        503: problem_openapi_response(
            "document_upload_unavailable",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
    },
    tags=["documents"],
)
async def get_document_upload(
    request: Request,
    project_id: UUID,
    upload_key: UUID,
    actor: ProjectReadActor,
) -> DocumentUploadResponse:
    """現在の原 actor/Project 認可で持続 upload を読むだけとし、PUT を再開しない。"""

    if upload_key.int == 0:
        raise _invalid_upload_key()
    service: DocumentService = request.app.state.document_service
    try:
        upload: StoredDocumentUpload = await service.get_upload(
            project_id=project_id, upload_key=upload_key, access=user_access(request, actor)
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except DocumentUploadNotFoundError as error:
        raise _document_upload_not_found() from error
    except (DocumentUploadInvalidError, FileStorageError) as error:
        raise _document_upload_unavailable() from error
    try:
        response = DocumentUploadResponse(
            upload_key=upload.upload_key,
            project_id=upload.project_id,
            state=upload.state,
            created_at=upload.created_at,
            document=_document_response(upload.document) if upload.document is not None else None,
        )
    except ValidationError as error:
        raise _document_upload_unavailable() from error
    if (
        response.upload_key != upload_key
        or response.project_id != project_id
        or (response.state == "PUBLISHED") != (response.document is not None)
        or (response.document is not None and response.document.project_id != project_id)
    ):
        raise _document_upload_unavailable()
    return response


@router.post(
    "/projects/{project_id}/document-uploads/{upload_key}/closure",
    response_model=DocumentUploadClosureResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {
            "description": "Publication closed; storage and quota remain unsettled",
            "headers": NO_STORE_PROBLEM_HEADERS,
        },
        200: {
            "model": DocumentUploadClosureResponse,
            "description": "The same original closure receipt; no new operation",
            "headers": NO_STORE_PROBLEM_HEADERS,
        },
        **_ACCESS_PROBLEMS,
        404: problem_openapi_response(
            "project_not_found or document_upload_not_found",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        409: problem_openapi_response(
            "project_archived or document_upload_already_published; no document is deleted",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        422: problem_openapi_response(
            "invalid_document_upload_key or invalid explicit confirmation",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        503: problem_openapi_response(
            "document_upload_unavailable; closure may be unknown",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
    },
    tags=["documents"],
)
async def close_document_upload(
    request: Request,
    response: Response,
    project_id: UUID,
    upload_key: UploadClosureKey,
    body: DocumentUploadClosureRequest,
    actor: ProjectWriteActor,
) -> DocumentUploadClosureResponse:
    """原作者の明示確認を独立 transaction へ渡し、再要求でも元の閉鎖回执を返す。"""

    del body
    key = _parse_upload_key(upload_key)
    service: DocumentService = request.app.state.document_service
    try:
        closure, created = await service.close_upload(
            project_id=project_id,
            upload_key=key,
            access=user_access(request, actor),
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except DocumentUploadNotFoundError as error:
        raise _document_upload_not_found() from error
    except DocumentUploadAlreadyPublishedError as error:
        raise ProblemException(
            status=409,
            title="Document upload already published",
            detail="The original upload was already published; no document was deleted.",
            code="document_upload_already_published",
        ) from error
    except (DocumentUploadInvalidError, SQLAlchemyError, ConnectionError, TimeoutError) as error:
        # commit 応答未知も固定 Problem に閉じる。取消は伝播し、成功や自動再送に変えない。
        raise _document_upload_unavailable() from error
    result = _upload_closure_response(closure, project_id=project_id, upload_key=key)
    if type(created) is not bool:
        raise _document_upload_unavailable()
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return result


@router.get(
    "/projects/{project_id}/document-uploads/{upload_key}/closure",
    response_model=DocumentUploadClosureResponse,
    responses={
        200: {
            "description": "Original publication closure, not a cleanup or quota receipt",
            "headers": NO_STORE_PROBLEM_HEADERS,
        },
        **_ACCESS_PROBLEMS,
        404: problem_openapi_response(
            "project_not_found or document_upload_closure_not_found; absence does not prove "
            "an earlier closure POST cannot still commit",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        422: problem_openapi_response(
            "invalid_document_upload_key",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        503: problem_openapi_response(
            "document_upload_unavailable",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
    },
    tags=["documents"],
)
async def get_document_upload_closure(
    request: Request,
    project_id: UUID,
    upload_key: UploadClosureKey,
    actor: ProjectReadActor,
) -> DocumentUploadClosureResponse:
    """帰档も現在の原作者に独立回执を投影し、閉鎖や PUT を再送しない。"""

    key = _parse_upload_key(upload_key)
    service: DocumentService = request.app.state.document_service
    try:
        closure = await service.get_upload_closure(
            project_id=project_id,
            upload_key=key,
            access=user_access(request, actor),
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except DocumentUploadClosureNotFoundError as error:
        raise ProblemException(
            status=404,
            title="Document upload closure not found",
            detail="The original publication closure is not available in this project.",
            code="document_upload_closure_not_found",
        ) from error
    except (DocumentUploadInvalidError, SQLAlchemyError, ConnectionError, TimeoutError) as error:
        raise _document_upload_unavailable() from error
    return _upload_closure_response(closure, project_id=project_id, upload_key=key)


def _upload_closure_response(
    closure: StoredDocumentUploadClosure,
    *,
    project_id: UUID,
    upload_key: UUID,
) -> DocumentUploadClosureResponse:
    """独立回执を白名単で変換し、壊れた原対象や日時は固定の拒否へ閉じる。"""

    try:
        # Pydantic の日時/UUID coercion で壊れた内部 DTO を有効な回执へ補正しない。
        if not isinstance(closure.closed_at, datetime) or any(
            not isinstance(value, UUID)
            for value in (closure.upload_key, closure.project_id, closure.document_id)
        ):
            raise _document_upload_unavailable()
        response = DocumentUploadClosureResponse(
            upload_key=closure.upload_key,
            project_id=closure.project_id,
            document_id=closure.document_id,
            closed_at=closure.closed_at,
            publication_state=closure.publication_state,
        )
    except (ValidationError, AttributeError) as error:
        raise _document_upload_unavailable() from error
    if response.project_id != project_id or response.upload_key != upload_key:
        raise _document_upload_unavailable()
    return response


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
            "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
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
        raise _document_storage_unavailable() from error
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
        503: problem_openapi_response(
            "document_storage_unavailable or document_upload_unavailable; "
            "metadata deletion may already have committed",
            headers=NO_STORE_PROBLEM_HEADERS,
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
    except DocumentUploadInvalidError as error:
        raise _document_upload_unavailable() from error
    except (DocumentStorageUnavailableError, FileStorageError) as error:
        raise _document_storage_unavailable() from error
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


def _document_storage_unavailable() -> ProblemException:
    """保存先の拒否/不明と SDK の私的詳細を同じ固定 Problem へ閉じる。"""

    return ProblemException(
        status=503,
        title="Document storage unavailable",
        detail="Document storage is currently unavailable",
        code="document_storage_unavailable",
    )


def _upload_key_header(request: Request) -> UUID:
    """認可後・正文前に単一の UUID を要求し、重複や曖昧な表記で別意図を作らない。"""

    values = request.headers.getlist("Idempotency-Key")
    if len(values) != 1:
        raise _invalid_upload_key()
    return _parse_upload_key(values[0])


def _parse_upload_key(value: str) -> UUID:
    """header と閉鎖 path で同じ原 UUID 規則を使い、入力値をエラーに反映しない。"""

    if len(value) != 36:
        raise _invalid_upload_key()
    try:
        key = UUID(value)
    except ValueError as error:
        raise _invalid_upload_key() from error
    if key.int == 0 or str(key) != value.lower():
        raise _invalid_upload_key()
    return key


def _invalid_upload_key() -> ProblemException:
    """元の header 値を反映しない固定の upload identity 拒否を返す。"""

    return ProblemException(
        status=422,
        title="Invalid document upload key",
        detail="The upload key must be a single non-zero UUID.",
        code="invalid_document_upload_key",
    )


def _document_upload_unavailable() -> ProblemException:
    """壊れた持続記録を修復・公開せず、内部値を固定 Problem へ閉じる。"""

    return ProblemException(
        status=503,
        title="Document upload unavailable",
        detail="The original document upload record is currently unavailable.",
        code="document_upload_unavailable",
    )


def _document_upload_not_found() -> ProblemException:
    """原 upload の不存在と別作者の key を同じ固定拒否にする。"""

    return ProblemException(
        status=404,
        title="Document upload not found",
        detail="The original upload is not available in this project.",
        code="document_upload_not_found",
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
