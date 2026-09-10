"""Project 文書の upload/列挙/download/削除 use case と安全境界を実装する。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.core.hashing import sha256_hex
from projectmind.db.errors import matches_constraint
from projectmind.documents.content import read_document_bytes, require_document_storage
from projectmind.documents.domain import (
    DocumentCleanupActor,
    DocumentConflictError,
    DocumentInUseError,
    DocumentNotFoundError,
    DocumentReferencesUnavailableError,
    DocumentStorageUnavailableError,
    DocumentUploadClosedError,
    DocumentUploadClosureNotFoundError,
    DocumentUploadError,
    DocumentUploadInvalidError,
    DocumentUploadKeyConflictError,
    DocumentUploadNotFoundError,
    DocumentUploadPendingError,
    StoredDocument,
    StoredDocumentUpload,
    StoredDocumentUploadClosure,
    UploadDocumentCommand,
)
from projectmind.documents.paths import document_storage_key, validate_document_path
from projectmind.documents.reference_repository import DocumentReferenceRepository
from projectmind.documents.repository import DocumentRepository
from projectmind.documents.upload_intent import upload_request_checksum
from projectmind.documents.upload_repository import DocumentUploadRepository
from projectmind.projects.domain import ProjectNotFoundError
from projectmind.projects.repository import ProjectRepository
from projectmind.storage import (
    BlobReference,
    FileStorage,
    FileStorageError,
    UploadLimits,
    UploadRejectedError,
)
from projectmind.users.access import authorize_user_access, validate_user_access
from projectmind.users.domain import UserAccess
from projectmind.users.repository import UserRepository, authorization_failure_snapshot


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

        return validate_document_path(project_id=project_id, folder=folder, name=name)

    async def upload_document(
        self,
        *,
        project_id: UUID,
        upload_key: UUID,
        access: UserAccess,
        folder: str,
        name: str,
        data: bytes,
        content_type: str,
    ) -> StoredDocument:
        """原要求と占用を先に commit し、その受付だけに一度の PUT と公開を許す。"""

        if not isinstance(access, UserAccess):
            raise TypeError("Document upload requires the original user access")
        validate_user_access(access)
        _validate_upload_key(upload_key)
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
        storage_key = document_storage_key(project_id, document_id, safe_name)
        reference = BlobReference(storage_key, self._file_storage.namespace)
        checksum = f"sha256:{sha256_hex(payload)}"
        request_checksum = upload_request_checksum(
            organization_id=access.actor.organization_id,
            project_id=project_id,
            actor_id=access.actor.user_id,
            folder=safe_folder,
            name=safe_name,
            size=len(payload),
            mime=normalized_mime,
            checksum=checksum,
        )
        async with self._upload_transaction(access, project_id) as transaction:
            repository, uploads, session_id = transaction
            original = await uploads.find(
                organization_id=access.actor.organization_id,
                project_id=project_id,
                actor_id=access.actor.user_id,
                upload_key=upload_key,
            )
            if original is not None:
                if original.request_checksum != request_checksum:
                    raise DocumentUploadKeyConflictError("Original upload content differs")
                if original.publication_closed_at is not None:
                    raise DocumentUploadClosedError("Original upload publication is closed")
                if original.receipt.state == "PENDING":
                    raise DocumentUploadPendingError("Original upload is still unresolved")
                if original.receipt.document is None:
                    raise DocumentUploadInvalidError("Original upload receipt is unavailable")
                # 目録が既に消えていても原公開回执を返す。現在の namespace は要求しない。
                return original.receipt.document
            if await repository.path_exists(
                project_id=project_id,
                folder=safe_folder,
                name=safe_name,
            ) or await uploads.path_reserved(
                project_id=project_id,
                folder=safe_folder,
                name=safe_name,
            ):
                raise DocumentConflictError("Document with the same path already exists")
            namespace = require_document_storage(self._file_storage, reference)
            self._limits.validate(
                size=len(payload),
                content_type=normalized_mime,
                content=payload,
                project_usage_bytes=await repository.project_usage_bytes(project_id),
            )
            command = UploadDocumentCommand(
                project_id=project_id,
                document_id=document_id,
                folder=safe_folder,
                name=safe_name,
                storage_key=storage_key,
                storage_namespace=namespace,
                upload_intent_id=uuid4(),
                size=len(payload),
                mime=normalized_mime,
                checksum=checksum,
                uploaded_by=access.actor.user_id,
            )
            admitted = uploads.reserve(
                upload_key=upload_key,
                organization_id=access.actor.organization_id,
                original_request_id=access.request_id,
                original_session_id=session_id,
                command=command,
                now=datetime.now(UTC),
            )
        # 受付 commit 未知ならここへ進まない。取消も予約解放や補償 DELETE に変換しない。
        require_document_storage(self._file_storage, reference)
        blob = await self._file_storage.put(storage_key, payload, content_type=normalized_mime)
        require_document_storage(self._file_storage, reference)
        if (
            blob.key != storage_key
            or type(blob.size) is not int
            or blob.size != len(payload)
            or blob.content_type != normalized_mime
            or blob.sha256 != checksum
        ):
            raise FileStorageError("Document storage acknowledgement did not match the upload")
        async with self._upload_transaction(access, project_id) as transaction:
            repository, uploads, session_id = transaction
            original = await uploads.find(
                organization_id=access.actor.organization_id,
                project_id=project_id,
                actor_id=access.actor.user_id,
                upload_key=upload_key,
            )
            if original is not None and original.publication_closed_at is not None:
                raise DocumentUploadClosedError("Original upload publication is closed")
            if original != admitted or session_id != admitted.original_session_id:
                raise DocumentUploadInvalidError("Original upload reservation changed")
            # 予約は既に計上済み。旧 writer の追加分も含むが同じ size を再加算しない。
            usage = await repository.project_usage_bytes(project_id)
            if usage > self._limits.project_quota_bytes:
                raise UploadRejectedError(
                    "project_quota_exceeded",
                    "Project storage quota is exceeded",
                )
            require_document_storage(self._file_storage, reference)
            document = await repository.create(command)
            await uploads.publish(admitted, document)
            return document

    async def get_upload(
        self,
        *,
        project_id: UUID,
        upload_key: UUID,
        access: UserAccess,
    ) -> StoredDocumentUpload:
        """現在の同一 actor に原受付を投影する。未検出は旧 POST の未到達を証明しない。"""

        if not isinstance(access, UserAccess):
            raise TypeError("Document upload lookup requires the current user access")
        validate_user_access(access)
        _validate_upload_key(upload_key)
        async with self._upload_transaction(access, project_id, write=False) as transaction:
            _, uploads, _ = transaction
            original = await uploads.find(
                organization_id=access.actor.organization_id,
                project_id=project_id,
                actor_id=access.actor.user_id,
                upload_key=upload_key,
            )
            if original is None:
                raise DocumentUploadNotFoundError("Original upload was not found")
            return original.receipt

    async def close_upload(
        self,
        *,
        project_id: UUID,
        upload_key: UUID,
        access: UserAccess,
    ) -> tuple[StoredDocumentUploadClosure, bool]:
        """現在の原作者が未公開 intent を閉じる。原 PUT の停止や quota 返却は行わない。"""

        if not isinstance(access, UserAccess):
            raise TypeError("Document upload closure requires the current user access")
        validate_user_access(access)
        _validate_upload_key(upload_key)
        async with self._upload_transaction(access, project_id) as transaction:
            _, uploads, session_id = transaction
            original = await uploads.find(
                organization_id=access.actor.organization_id,
                project_id=project_id,
                actor_id=access.actor.user_id,
                upload_key=upload_key,
            )
            if original is None:
                raise DocumentUploadNotFoundError("Original upload was not found")
            # 新しい有効な会話による独立の閉鎖。旧 PUT を新会話へ引き継がない。
            return await uploads.close_publication(
                original,
                actor=DocumentCleanupActor(
                    organization_id=access.actor.organization_id,
                    actor_id=access.actor.user_id,
                    request_id=access.request_id,
                    session_id=session_id,
                ),
                now=datetime.now(UTC),
            )

    async def get_upload_closure(
        self,
        *,
        project_id: UUID,
        upload_key: UUID,
        access: UserAccess,
    ) -> StoredDocumentUploadClosure:
        """原作者の現会話で閉鎖回执だけを確認する。未検出でも旧 POST を再送しない。"""

        if not isinstance(access, UserAccess):
            raise TypeError("Document upload closure lookup requires the current user access")
        validate_user_access(access)
        _validate_upload_key(upload_key)
        async with self._upload_transaction(access, project_id, write=False) as transaction:
            _, uploads, _ = transaction
            original = await uploads.find(
                organization_id=access.actor.organization_id,
                project_id=project_id,
                actor_id=access.actor.user_id,
                upload_key=upload_key,
            )
            closure = None if original is None else await uploads.get_closure(original)
            if closure is None:
                raise DocumentUploadClosureNotFoundError("Original upload closure was not found")
            return closure

    @asynccontextmanager
    async def _upload_transaction(
        self,
        access: UserAccess,
        project_id: UUID,
        *,
        write: bool = True,
    ) -> AsyncIterator[tuple[DocumentRepository, DocumentUploadRepository, UUID]]:
        """書込は原会話、核対は現在の会話を固定し、全結果を最終認可 gate に通す。"""

        async with self._session_factory() as session, session.begin():
            users = await UserRepository(session).lock_users(
                access=access, target_id=None, include_target_sessions=False, read_only_actor=True
            )
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=write)
            projects = ProjectRepository(session)
            try:
                project = await projects.lock_write_access(user=users.actor, project_id=project_id)
            except ProjectNotFoundError:
                authorize_user_access(
                    access,
                    users,
                    now=datetime.now(UTC),
                    admin=False,
                    write=write,
                )
                raise

            def require_current_access() -> None:
                """全待機と flush 後に新しい時刻で原資格を判定する。"""

                authorize_user_access(
                    access,
                    users,
                    now=datetime.now(UTC),
                    admin=False,
                    write=write,
                )
                if write:
                    projects.require_active_write_access(project)
                else:
                    projects.require_read_access(project)

            require_current_access()
            failure_snapshot = authorization_failure_snapshot(users)
            try:
                yield (
                    DocumentRepository(session),
                    DocumentUploadRepository(session),
                    users.current_session.id,
                )
                require_current_access()
                await session.flush()
                require_current_access()
            except (
                UploadRejectedError,
                DocumentConflictError,
                DocumentStorageUnavailableError,
                DocumentUploadError,
            ):
                require_current_access()
                raise
            except IntegrityError as error:
                if write and matches_constraint(error, "uq_project_documents_project_folder_name"):
                    # failed flush は原 ORM を expire し得る。snapshot は失敗分類だけに使う。
                    authorize_user_access(
                        access,
                        failure_snapshot,
                        now=datetime.now(UTC),
                        admin=False,
                        write=True,
                    )
                    raise DocumentConflictError(
                        "Document with the same path already exists",
                    ) from error
                raise

    async def list_documents(self, *, project_id: UUID) -> list[StoredDocument]:
        """Project 内文書 metadata の一覧を取得する。"""

        async with self._session_factory() as session:
            return await DocumentRepository(session).list_for_project(project_id)

    async def download_document(
        self, *, project_id: UUID, document_id: UUID
    ) -> tuple[StoredDocument, bytes]:
        """所有確認済み metadata と一致する同一読取の blob 正文だけを返す。"""

        async with self._session_factory() as session:
            document, reference = await DocumentRepository(session).get_for_download(
                project_id=project_id, document_id=document_id
            )
        content = await read_document_bytes(
            self._file_storage, reference=reference, document=document
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
                _, reference = await repository.get_for_download(
                    project_id=project_id, document_id=document_id
                )
                require_document_storage(self._file_storage, reference)
            except (
                DocumentNotFoundError,
                DocumentInUseError,
                DocumentReferencesUnavailableError,
                DocumentStorageUnavailableError,
            ):
                require_current_access()
                raise
            require_current_access()
            try:
                deleted_reference = await repository.delete(
                    project_id=project_id,
                    document_id=document_id,
                    cleanup_actor=DocumentCleanupActor(
                        organization_id=access.actor.organization_id,
                        actor_id=access.actor.user_id,
                        request_id=access.request_id,
                        session_id=users.current_session.id,
                    ),
                )
            except (DocumentUploadError, DocumentStorageUnavailableError):
                require_current_access()
                raise
            if deleted_reference != reference:
                raise DocumentStorageUnavailableError("Document storage reference changed")
            await session.flush()
            require_current_access()
        # 清理要求は commit 済みでも、無条件 PUT の終了証明が無いので占用は解放しない。
        require_document_storage(self._file_storage, reference)
        await self._file_storage.delete(reference.key)


def _validate_upload_key(upload_key: UUID) -> None:
    """HTTP 以外の呼出元にも原要求の非 nil UUID を要求する。"""

    if not isinstance(upload_key, UUID) or upload_key.int == 0:
        raise UploadRejectedError("invalid_document_upload_key", "A valid upload key is required")
