"""Project 文書の upload/列挙/download/削除 use case と安全境界を実装する。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.core.hashing import sha256_hex
from projectmind.db.errors import matches_constraint
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
from projectmind.storage import (
    FileStorage,
    FileStorageError,
    UploadLimits,
    UploadRejectedError,
    sanitize_object_key,
)
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

    @property
    def max_upload_bytes(self) -> int:
        """HTTP の有界読取にも同じ一文書 byte 上限を提供する。"""

        return self._limits.max_bytes

    @staticmethod
    def validate_upload_path(*, project_id: UUID, folder: str, name: str) -> tuple[str, str]:
        """本文取得前にも共有 storage key 規則を適用し、長い名前を切り詰めない。"""

        safe_folder, safe_name = _safe_folder(folder), _safe_name(name)
        _document_storage_key(project_id, UUID(int=0), safe_name)
        return safe_folder, safe_name

    async def upload_document(
        self,
        *,
        project_id: UUID,
        access: UserAccess,
        folder: str,
        name: str,
        data: bytes,
        content_type: str,
    ) -> StoredDocument:
        """原資格を storage 前後で再検証し、公開 metadata の quota を最終 lock 内で守る。"""

        if not isinstance(access, UserAccess):
            raise TypeError("Document upload requires the original user access")
        validate_user_access(access)
        # 呼出元の可変 buffer を await 後に再利用せず、検査/保存する本文を同一にする。
        payload = bytes(data)
        safe_folder, safe_name = self.validate_upload_path(
            project_id=project_id, folder=folder, name=name
        )
        normalized_mime = self._limits.validate(
            size=len(payload),
            content_type=content_type,
            content=payload,
            project_usage_bytes=0,
        )
        document_id = uuid4()
        storage_key = _document_storage_key(project_id, document_id, safe_name)
        checksum = f"sha256:{sha256_hex(payload)}"
        async with self._upload_transaction(access, project_id) as repository:
            self._limits.validate(
                size=len(payload), content_type=normalized_mime, content=payload,
                project_usage_bytes=await repository.project_usage_bytes(project_id),
            )
        # 初回事前確認は永続予約ではない。外部 I/O の間は Org/User/Project lock を保持しない。
        blob = await self._file_storage.put(storage_key, payload, content_type=normalized_mime)
        if (
            blob.key != storage_key
            or type(blob.size) is not int
            or blob.size != len(payload)
            or blob.content_type != normalized_mime
            or blob.sha256 != checksum
        ):
            raise FileStorageError("Document storage acknowledgement did not match the upload")
        command = UploadDocumentCommand(
            project_id=project_id,
            document_id=document_id,
            folder=safe_folder,
            name=safe_name,
            storage_key=storage_key,
            size=len(payload),
            mime=normalized_mime,
            checksum=checksum,
            uploaded_by=access.actor.user_id,
        )
        # 拒否/取消/commit 未知の blob は補償削除しない。永続予約と cleanup 回復は未実装。
        try:
            async with self._upload_transaction(access, project_id) as repository:
                self._limits.validate(
                    size=len(payload), content_type=normalized_mime, content=payload,
                    project_usage_bytes=await repository.project_usage_bytes(project_id),
                )
                return await repository.create(command)
        except IntegrityError as error:
            if matches_constraint(error, "uq_project_documents_project_folder_name"):
                raise DocumentConflictError("Document with the same path already exists") from error
            raise

    @asynccontextmanager
    async def _upload_transaction(
        self, access: UserAccess, project_id: UUID,
    ) -> AsyncIterator[DocumentRepository]:
        """両方の短い transaction で同じ原会話と現在の Project を固定する。"""

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
                """全待機と flush 後に新しい時刻で原資格を判定する。"""

                authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
                projects.require_active_write_access(project)

            require_current_access()
            try:
                yield DocumentRepository(session)
            except (UploadRejectedError, DocumentConflictError):
                require_current_access()
                raise
            require_current_access()
            await session.flush()
            require_current_access()

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


def _document_storage_key(project_id: UUID, document_id: UUID, name: str) -> str:
    """storage の単段上限を別定義せず、書込前の安定した名前拒否へ変換する。"""

    try:
        return sanitize_object_key(f"projects/{project_id}/documents/{document_id}/{name}")
    except FileStorageError as error:
        raise UploadRejectedError(
            "invalid_document_name", "Document name cannot be stored safely"
        ) from error


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
