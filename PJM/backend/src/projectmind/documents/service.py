"""Project 文書の upload/列挙/download/削除 use case と安全境界を実装する。"""

from __future__ import annotations

from pathlib import PurePosixPath
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.documents.domain import (
    DocumentConflictError,
    StoredDocument,
    UploadDocumentCommand,
)
from projectmind.documents.repository import DocumentRepository
from projectmind.storage import FileStorage, UploadLimits, UploadRejectedError

_NAME_MAX = 200
_FOLDER_MAX = 200


class DocumentService:
    """Transaction 境界と upload 安全 policy を所有する文書 use case。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        file_storage: FileStorage,
        limits: UploadLimits,
    ) -> None:
        """Database factory、object storage、upload 上限 policy を保持する。"""

        self._session_factory = session_factory
        self._file_storage = file_storage
        self._limits = limits

    async def upload_document(
        self,
        *,
        project_id: UUID,
        uploaded_by: UUID,
        folder: str,
        name: str,
        data: bytes,
        content_type: str,
    ) -> StoredDocument:
        """安全化・上限・機密走査を経て blob を保存し、metadata を永続化する。"""

        safe_folder = _safe_folder(folder)
        safe_name = _safe_name(name)
        async with self._session_factory() as session:
            usage = await DocumentRepository(session).project_usage_bytes(project_id)
        # size/種別/配額/機密を storage 前に fail-closed で検証する (F1 の共通 policy を再利用)。
        normalized_mime = self._limits.validate(
            size=len(data),
            content_type=content_type,
            content=data,
            project_usage_bytes=usage,
        )
        document_id = uuid4()
        storage_key = f"projects/{project_id}/documents/{document_id}/{safe_name}"
        blob = await self._file_storage.put(storage_key, data, content_type=normalized_mime)
        command = UploadDocumentCommand(
            project_id=project_id,
            document_id=document_id,
            folder=safe_folder,
            name=safe_name,
            storage_key=blob.key,
            size=blob.size,
            mime=normalized_mime,
            checksum=blob.sha256,
            uploaded_by=uploaded_by,
        )
        try:
            async with self._session_factory() as session, session.begin():
                return await DocumentRepository(session).create(command)
        except IntegrityError as error:
            # 同一 folder/name の一意制約違反は安定した conflict として閉じる。
            raise DocumentConflictError(
                f"Document already exists: {safe_folder}/{safe_name}"
            ) from error

    async def list_documents(self, *, project_id: UUID) -> list[StoredDocument]:
        """Project 内文書 metadata の一覧を取得する。"""

        async with self._session_factory() as session:
            return await DocumentRepository(session).list_for_project(project_id)

    async def download_document(
        self, *, project_id: UUID, document_id: UUID
    ) -> tuple[StoredDocument, bytes]:
        """所有確認済みの文書 metadata と blob 正文を返す。"""

        async with self._session_factory() as session:
            document, storage_key = await DocumentRepository(session).get_for_download(
                project_id=project_id, document_id=document_id
            )
        content = await self._file_storage.get(storage_key)
        return document, content

    async def delete_document(self, *, project_id: UUID, document_id: UUID) -> None:
        """所有確認後に metadata を削除し、object storage の blob も削除する。"""

        async with self._session_factory() as session, session.begin():
            storage_key = await DocumentRepository(session).delete(
                project_id=project_id, document_id=document_id
            )
        await self._file_storage.delete(storage_key)


def _safe_name(value: str) -> str:
    """文書名を単一の安全な file 名に限定する。path segment は許可しない。"""

    candidate = value.strip()
    if not candidate or len(candidate) > _NAME_MAX:
        raise UploadRejectedError("invalid_document_name", "Document name is empty or too long")
    if candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
        raise UploadRejectedError("invalid_document_name", "Document name must be a single segment")
    if not candidate.isprintable():
        raise UploadRejectedError(
            "invalid_document_name", "Document name has unprintable characters"
        )
    return candidate


def _safe_folder(value: str) -> str:
    """Folder を空 (root) か安全な相対 POSIX path に限定する。"""

    candidate = value.strip().strip("/")
    if not candidate:
        return ""
    if len(candidate) > _FOLDER_MAX or "\\" in candidate:
        raise UploadRejectedError("invalid_document_folder", "Document folder is invalid")
    path = PurePosixPath(candidate)
    if path.is_absolute():
        raise UploadRejectedError("invalid_document_folder", "Document folder must be relative")
    for part in path.parts:
        if part in {"", ".", ".."} or not part.isprintable():
            raise UploadRejectedError(
                "invalid_document_folder", "Document folder has an unsafe segment"
            )
    return path.as_posix()
