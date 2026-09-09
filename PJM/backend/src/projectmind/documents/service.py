"""Project 文書の upload/列挙/download/削除 use case と安全境界を実装する。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.documents.content import read_document_bytes
from projectmind.documents.domain import (
    DocumentConflictError,
    DocumentInUseError,
    DocumentNotFoundError,
    DocumentReferencesUnavailableError,
    StoredDocument,
    UploadDocumentCommand,
)
from projectmind.documents.reference_repository import DocumentReferenceRepository
from projectmind.documents.repository import DocumentRepository
from projectmind.projects.domain import ProjectNotFoundError
from projectmind.projects.repository import ProjectRepository
from projectmind.storage import FileStorage, UploadLimits, UploadRejectedError
from projectmind.users.access import authorize_user_access, validate_user_access
from projectmind.users.domain import UserAccess
from projectmind.users.repository import UserRepository

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
        """所有確認済み metadata と一致する同一読取の blob 正文だけを返す。"""

        async with self._session_factory() as session:
            document, storage_key = await DocumentRepository(session).get_for_download(
                project_id=project_id, document_id=document_id
            )
        content = await read_document_bytes(
            self._file_storage, storage_key=storage_key, document=document
        )
        return document, content

    async def get_document(self, *, project_id: UUID, document_id: UUID) -> StoredDocument:
        """原 ID の現在の metadata だけを読み、blob 清理や原 DELETE の成功を推測しない。"""

        async with self._session_factory() as session:
            return await DocumentRepository(session).get(
                project_id=project_id, document_id=document_id
            )

    async def delete_document(
        self, *, project_id: UUID, document_id: UUID, access: UserAccess
    ) -> None:
        """原資格と参照を同じ門禁で確認し、commit が確認できてから blob 削除を試みる。"""

        if not isinstance(access, UserAccess):
            raise TypeError("Document deletion requires the original user access")
        validate_user_access(access)
        async with self._session_factory() as session, session.begin():
            users = await UserRepository(session).lock_users(
                access=access, target_id=None, include_target_sessions=False, read_only_actor=True
            )
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
            projects = ProjectRepository(session)
            try:
                project = await projects.lock_write_access(user=users.actor, project_id=project_id)
            except ProjectNotFoundError:
                authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
                raise

            def require_current_access() -> None:
                """lock/SELECT/flush 待機後の新時刻で、原会話を先に再確認する。"""

                authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
                projects.require_active_write_access(project)

            require_current_access()
            repository = DocumentRepository(session)
            try:
                await repository.lock_for_deletion(project_id=project_id, document_id=document_id)
                require_current_access()
                await DocumentReferenceRepository(session).require_unreferenced(
                    project_id=project_id, document_id=document_id
                )
            except (DocumentNotFoundError, DocumentInUseError, DocumentReferencesUnavailableError):
                require_current_access()
                raise
            require_current_access()
            storage_key = await repository.delete(project_id=project_id, document_id=document_id)
            await session.flush()
            require_current_access()
        # DB/取消/commit 未知を成功に変換しない。永続 cleanup 回执は別の未完了境界。
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
