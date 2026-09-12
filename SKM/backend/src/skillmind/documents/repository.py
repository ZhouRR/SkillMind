"""Project 作用域の文書 metadata 永続化を実装する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import BigInteger, case, cast, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import (
    ProjectDocument,
    ProjectDocumentCleanup,
    ProjectDocumentEffectUpload,
    ProjectDocumentUpload,
)
from skillmind.documents.cleanup_repository import DocumentCleanupRepository
from skillmind.documents.domain import (
    DocumentCleanupActor,
    DocumentInUseError,
    DocumentNotFoundError,
    DocumentStorageUnavailableError,
    DocumentUploadInvalidError,
    StoredDocument,
    UploadDocumentCommand,
)
from skillmind.documents.upload_repository import DocumentUploadRepository
from skillmind.storage.blob import BlobReference, FileStorageError, StorageNamespace


class DocumentRepository:
    """Transaction-scoped session 上で文書 metadata を読み書きする。"""

    def __init__(self, session: AsyncSession) -> None:
        """Transaction-scoped database session を保持する。"""

        self._session = session

    async def project_usage_bytes(self, project_id: UUID) -> int:
        """未公開・公開・清理待ちを同じ占用とし、旧目録だけを別途加算する。"""

        legacy_scope = (
            ProjectDocument.project_id == project_id,
            ProjectDocument.upload_intent_id.is_(None),
            ProjectDocument.effect_upload_id.is_(None),
        )
        legacy = select(func.coalesce(func.sum(ProjectDocument.size), 0)).where(
            *legacy_scope,
        ).scalar_subquery()
        reserved = select(func.coalesce(func.sum(ProjectDocumentUpload.size), 0)).where(
            ProjectDocumentUpload.project_id == project_id,
        ).scalar_subquery()
        effects = select(func.coalesce(func.sum(ProjectDocumentEffectUpload.size), 0)).where(
            ProjectDocumentEffectUpload.project_id == project_id,
        ).scalar_subquery()
        legacy_cleanup = select(func.coalesce(func.sum(ProjectDocumentCleanup.size), 0)).where(
            ProjectDocumentCleanup.project_id == project_id,
            ProjectDocumentCleanup.upload_intent_id.is_(None),
        ).scalar_subquery()
        # 壊れた旧負数で新規枠を作らず、PG の numeric SUM を小数切捨てにも依存しない。
        statement = select(case(
            (exists().where(*legacy_scope, ProjectDocument.size < 0), None),
            else_=cast(legacy + reserved + effects + legacy_cleanup, BigInteger),
        ))
        value = await self._session.scalar(statement)
        if type(value) is not int or value < 0:
            raise DocumentUploadInvalidError("Document quota could not be verified")
        return value

    async def create(self, command: UploadDocumentCommand) -> StoredDocument:
        """文書 metadata 行を追加し、read model を返す。"""

        now = datetime.now(UTC)
        document = ProjectDocument(
            id=command.document_id,
            project_id=command.project_id,
            folder=command.folder,
            name=command.name,
            storage_key=command.storage_key,
            storage_namespace_id=command.storage_namespace.namespace_id,
            storage_descriptor_checksum=command.storage_namespace.descriptor_checksum,
            storage_is_durable=command.storage_namespace.durable,
            upload_intent_id=command.upload_intent_id,
            size=command.size,
            mime=command.mime,
            checksum=command.checksum,
            uploaded_by=command.uploaded_by,
            created_at=now,
        )
        self._session.add(document)
        return _to_stored(document)

    async def path_exists(self, *, project_id: UUID, folder: str, name: str) -> bool:
        """既存目録の保存先を解決せず、同じ表示 path への新しい PUT を拒否する。"""

        return bool(await self._session.scalar(select(exists().where(
            ProjectDocument.project_id == project_id,
            ProjectDocument.folder == folder,
            ProjectDocument.name == name,
        ))))

    async def list_for_project(self, project_id: UUID) -> list[StoredDocument]:
        """Project 内文書を folder/name 昇順で列挙する。"""

        statement = (
            select(ProjectDocument)
            .where(ProjectDocument.project_id == project_id)
            .order_by(ProjectDocument.folder, ProjectDocument.name)
        )
        return [_to_stored(row) for row in await self._session.scalars(statement)]

    async def find_by_path(
        self, *, project_id: UUID, folder: str, name: str
    ) -> tuple[StoredDocument, BlobReference] | None:
        """Project 作用域で展示 path に一致する文書と原保存先を返す。無ければ None。"""

        statement = select(ProjectDocument).where(
            ProjectDocument.project_id == project_id,
            ProjectDocument.folder == folder,
            ProjectDocument.name == name,
        )
        document = (await self._session.scalars(statement)).first()
        if document is None:
            return None
        return _to_stored(document), _blob_reference(document)

    async def get(self, *, project_id: UUID, document_id: UUID) -> StoredDocument:
        """所有 Project 内の文書 metadata を取得する。越権/不存在は 404 相当へ畳む。"""

        return _to_stored(await self._require(project_id=project_id, document_id=document_id))

    async def get_for_download(
        self, *, project_id: UUID, document_id: UUID
    ) -> tuple[StoredDocument, BlobReference]:
        """Download 用に metadata と内部保存先を所有確認付きで返す。"""

        document = await self._require(project_id=project_id, document_id=document_id)
        return _to_stored(document), _blob_reference(document)

    async def delete(
        self, *, project_id: UUID, document_id: UUID, cleanup_actor: DocumentCleanupActor,
    ) -> BlobReference:
        """service の原会話・文書 lock と無参照確認後に、同一 transaction で行を除く。"""

        document = await self._require(project_id=project_id, document_id=document_id)
        if document.effect_upload_id is not None:
            raise DocumentInUseError("Document is referenced by a retained effect receipt")
        reference = _blob_reference(document)
        if document.upload_intent_id is not None:
            await DocumentUploadRepository(self._session).request_cleanup(
                intent_id=document.upload_intent_id, document=_to_stored(document),
                reference=reference, now=datetime.now(UTC),
            )
        DocumentCleanupRepository(self._session).request(
            actor=cleanup_actor, document=_to_stored(document), reference=reference,
            upload_intent_id=document.upload_intent_id,
        )
        await self._session.delete(document)
        return reference

    async def lock_for_deletion(self, *, project_id: UUID, document_id: UUID) -> StoredDocument:
        """組織・認証・Project の後で原 ID を锁定し、待機前の ORM 値を再利用しない。"""

        document = await self._session.scalar(
            select(ProjectDocument)
            .where(ProjectDocument.project_id == project_id, ProjectDocument.id == document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if document is None:
            raise DocumentNotFoundError("Document is not available in this project")
        return _to_stored(document)

    async def _require(self, *, project_id: UUID, document_id: UUID) -> ProjectDocument:
        """Project 所有を確認して文書行を返す。越権と不存在は同じ error へ畳む。"""

        document = await self._session.get(ProjectDocument, document_id)
        if document is None or document.project_id != project_id:
            raise DocumentNotFoundError(f"Document not found: {document_id}")
        return document


def _blob_reference(document: ProjectDocument) -> BlobReference:
    """旧行の所属を現在設定で補わず、部分破損を不明な保存先として拒否する。"""

    namespace_id = document.storage_namespace_id
    checksum = document.storage_descriptor_checksum
    durable = document.storage_is_durable
    if namespace_id is None and checksum is None and durable is None:
        return BlobReference(key=document.storage_key, namespace=None)
    if namespace_id is None or checksum is None or durable is None:
        raise DocumentStorageUnavailableError("Document storage namespace is unavailable")
    try:
        namespace = StorageNamespace(namespace_id, checksum, durable)
    except FileStorageError as error:
        raise DocumentStorageUnavailableError(
            "Document storage namespace is unavailable"
        ) from error
    return BlobReference(key=document.storage_key, namespace=namespace)


def _to_stored(document: ProjectDocument) -> StoredDocument:
    """ORM 行を storage 非依存の read model へ変換する。"""

    return StoredDocument(
        document_id=document.id,
        project_id=document.project_id,
        folder=document.folder,
        name=document.name,
        size=document.size,
        mime=document.mime,
        checksum=document.checksum,
        uploaded_by=document.uploaded_by,
        created_at=document.created_at,
    )
