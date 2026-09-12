"""Org 認可 gate 内で成果 upload の原予約・送信・核対・文書公開を保存する。

この repository は実行権を与えず、commit も object I/O もしない。呼出元は共有の
Effect 認可を復験し、Organization UPDATE lock を全 transaction 中保持する。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.artifacts.repository import ArtifactRepository
from skillmind.db.models import ProjectDocument, ProjectDocumentEffectUpload
from skillmind.documents.domain import (
    DocumentConflictError,
    DocumentUploadInvalidError,
    StoredDocument,
)
from skillmind.documents.paths import document_effect_storage_key
from skillmind.documents.repository import DocumentRepository
from skillmind.documents.upload_repository import DocumentUploadRepository
from skillmind.storage.effect_write import ObjectWriteCommand, ObjectWriteReceipt
from skillmind.storage.validation import UploadLimits


class DocumentEffectRepository:
    """元 Effect ごとの占用を残し、応答未知から送信権や未保存を推定しない。"""

    def __init__(self, session: AsyncSession) -> None:
        """Org/actor/Project/Effect の認可 transaction と同じ session を使う。"""

        self._session = session

    async def reserve(
        self,
        command: ObjectWriteCommand,
        *,
        organization_id: UUID,
        actor_id: UUID,
        folder: str,
        name: str,
        limits: UploadLimits,
        now: datetime,
    ) -> ProjectDocumentEffectUpload:
        """同 Run の原 Artifact と path/配額を確認し、再受付では元予約だけを返す。"""

        command.validate()
        if (
            not _aware(now)
            or any(
                not isinstance(value, UUID) or value.int == 0
                for value in (organization_id, actor_id)
            )
            or command.object_key != document_effect_storage_key(command.project_id, folder, name)
        ):
            raise _invalid()
        row = await self._find(command.effect_id)
        if row is not None:
            _validate(row, command)
            if (row.organization_id, row.actor_id, row.folder, row.name) != (
                organization_id,
                actor_id,
                folder,
                name,
            ):
                raise _invalid()
            return row
        artifact = await ArtifactRepository(self._session).get_content(
            project_id=command.project_id,
            run_id=command.run_id,
            artifact_ref=command.artifact_ref,
        )
        if artifact is None or artifact.content != command.content:
            raise _invalid()
        documents = DocumentRepository(self._session)
        if await documents.path_exists(project_id=command.project_id, folder=folder, name=name) or (
            await DocumentUploadRepository(self._session).path_reserved(
                project_id=command.project_id,
                folder=folder,
                name=name,
            )
        ):
            raise DocumentConflictError("Document with the same path already exists")
        limits.validate(
            size=len(command.content),
            content_type=command.content_type,
            content=command.content,
            project_usage_bytes=await documents.project_usage_bytes(command.project_id),
        )
        row = ProjectDocumentEffectUpload(
            id=uuid4(),
            effect_id=command.effect_id,
            project_id=command.project_id,
            run_id=command.run_id,
            organization_id=organization_id,
            actor_id=actor_id,
            artifact_ref=command.artifact_ref,
            document_id=uuid5(command.effect_id, "project-document"),
            protocol_version=1,
            request_checksum=command.request_checksum,
            folder=folder,
            name=name,
            bucket=command.bucket,
            storage_key=command.object_key,
            storage_namespace_id=command.namespace.namespace_id,
            storage_descriptor_checksum=command.namespace.descriptor_checksum,
            storage_is_durable=command.namespace.durable,
            size=len(command.content),
            mime=command.content_type,
            checksum=command.content_checksum,
            state="RESERVED",
            created_at=now,
            sent_at=None,
            verified_at=None,
            published_at=None,
            put_owner_id=None,
            etag=None,
            version_id=None,
        )
        _validate(row, command)
        self._session.add(row)
        return row

    async def start_once(
        self,
        command: ObjectWriteCommand,
        *,
        owner_id: UUID,
        now: datetime,
    ) -> bool:
        """初回だけ送信開始を記録する。True の transaction commit 確認前は送信不可。"""

        row = await self.require(command)
        if (
            not isinstance(owner_id, UUID)
            or owner_id.int == 0
            or not _aware(now)
            or now < row.created_at
        ):
            raise _invalid()
        if row.state != "RESERVED":
            return False
        row.state, row.sent_at, row.put_owner_id = "SENT", now, owner_id
        _validate(row, command)
        return True

    async def record_verified(
        self,
        command: ObjectWriteCommand,
        receipt: ObjectWriteReceipt,
        *,
        now: datetime,
    ) -> None:
        """原 client の核対済み事実だけを保存し、404/同名/申告 hash を回执にしない。"""

        row = await self.require(command)
        if not _aware(now) or row.sent_at is None or now < row.sent_at:
            raise _invalid()
        if (
            receipt.effect_id,
            receipt.request_checksum,
            receipt.object_key,
            receipt.content_checksum,
            receipt.size,
            receipt.content_type,
        ) != (
            command.effect_id,
            command.request_checksum,
            command.object_key,
            command.content_checksum,
            len(command.content),
            command.content_type,
        ):
            raise _invalid()
        if row.state in {"VERIFIED", "PUBLISHED"}:
            if (row.etag, row.version_id) != (receipt.etag, receipt.version_id):
                raise _invalid()
            return
        if row.state != "SENT":
            raise _invalid()
        row.state, row.verified_at = "VERIFIED", now
        row.etag, row.version_id = receipt.etag, receipt.version_id
        _validate(row, command)

    async def publish(self, command: ObjectWriteCommand, *, now: datetime) -> StoredDocument:
        """同一 transaction で目録と原公開回执を作り、既存/消去済み目録は再作成しない。"""

        row = await self.require(command)
        if row.state == "PUBLISHED":
            return _document(row)
        if (
            row.state != "VERIFIED"
            or not _aware(now)
            or row.verified_at is None
            or now < row.verified_at
        ):
            raise _invalid()
        if await DocumentRepository(self._session).path_exists(
            project_id=row.project_id,
            folder=row.folder,
            name=row.name,
        ):
            raise DocumentConflictError("Document with the same path already exists")
        # 原 byte 占用は予約に残す。ブラウザの upload key/session を偽造しない。
        self._session.add(
            ProjectDocument(
                id=row.document_id,
                project_id=row.project_id,
                folder=row.folder,
                name=row.name,
                upload_intent_id=None,
                effect_upload_id=row.id,
                storage_key=row.storage_key,
                storage_namespace_id=row.storage_namespace_id,
                storage_descriptor_checksum=row.storage_descriptor_checksum,
                storage_is_durable=row.storage_is_durable,
                size=row.size,
                mime=row.mime,
                checksum=row.checksum,
                uploaded_by=row.actor_id,
                created_at=now,
            )
        )
        row.state, row.published_at = "PUBLISHED", now
        _validate(row, command)
        return _document(row)

    async def require(self, command: ObjectWriteCommand) -> ProjectDocumentEffectUpload:
        """原 Effect の保存対象を lock 下で照合し、再開先の差替えを拒否する。"""

        command.validate()
        row = await self._find(command.effect_id)
        if row is None:
            raise _invalid()
        _validate(row, command)
        return row

    async def verified_receipt(self, command: ObjectWriteCommand) -> ObjectWriteReceipt | None:
        """保存済み原回执だけを返す。SENT や現在の同名 object から成功を補わない。"""

        row = await self.require(command)
        if row.state not in {"VERIFIED", "PUBLISHED"}:
            return None
        if row.etag is None:
            raise _invalid()
        return ObjectWriteReceipt(
            row.effect_id, row.request_checksum, row.storage_key, row.checksum,
            row.size, row.mime, row.etag, row.version_id,
        )

    async def _find(self, effect_id: UUID) -> ProjectDocumentEffectUpload | None:
        """Org gate の後で原予約だけを lock し、古い ORM cache を使用しない。"""

        row: ProjectDocumentEffectUpload | None = await self._session.scalar(
            select(ProjectDocumentEffectUpload)
            .where(
                ProjectDocumentEffectUpload.effect_id == effect_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return row


def _validate(row: ProjectDocumentEffectUpload, command: ObjectWriteCommand) -> None:
    """原 descriptor 全体と段階の対を検証し、壊れた timestamp を既定値で埋めない。"""

    if any(
        not isinstance(value, UUID) or value.int == 0
        for value in (
            row.id,
            row.organization_id,
            row.actor_id,
        )
    ) or (
        row.put_owner_id is not None
        and (not isinstance(row.put_owner_id, UUID) or row.put_owner_id.int == 0)
    ):
        raise _invalid()
    if (
        row.effect_id,
        row.project_id,
        row.run_id,
        row.artifact_ref,
        row.request_checksum,
        row.bucket,
        row.storage_key,
        row.storage_namespace_id,
        row.storage_descriptor_checksum,
        row.storage_is_durable,
        row.size,
        row.mime,
        row.checksum,
    ) != (
        command.effect_id,
        command.project_id,
        command.run_id,
        command.artifact_ref,
        command.request_checksum,
        command.bucket,
        command.object_key,
        command.namespace.namespace_id,
        command.namespace.descriptor_checksum,
        command.namespace.durable,
        len(command.content),
        command.content_type,
        command.content_checksum,
    ):
        raise _invalid()
    if (
        row.document_id != uuid5(command.effect_id, "project-document")
        or row.storage_key != document_effect_storage_key(row.project_id, row.folder, row.name)
        or type(row.protocol_version) is not int
        or row.protocol_version != 1
        or type(row.size) is not int
        or row.size <= 0
        or not _aware(row.created_at)
        or row.state not in {"RESERVED", "SENT", "VERIFIED", "PUBLISHED"}
    ):
        raise _invalid()
    previous = row.created_at
    for value, required in (
        (row.sent_at, row.state != "RESERVED"),
        (row.verified_at, row.state in {"VERIFIED", "PUBLISHED"}),
        (row.published_at, row.state == "PUBLISHED"),
    ):
        if required:
            if not _aware(value) or value is None or value < previous:
                raise _invalid()
            previous = value
        elif value is not None:
            raise _invalid()
    if (row.state == "RESERVED") != (row.put_owner_id is None):
        raise _invalid()
    if row.state in {"RESERVED", "SENT"}:
        if row.etag is not None or row.version_id is not None:
            raise _invalid()
    else:
        if row.etag is None:
            raise _invalid()
        try:
            ObjectWriteReceipt(
                row.effect_id,
                row.request_checksum,
                row.storage_key,
                row.checksum,
                row.size,
                row.mime,
                row.etag,
                row.version_id,
            )
        except ValueError as error:
            raise _invalid() from error


def _document(row: ProjectDocumentEffectUpload) -> StoredDocument:
    """現在の目録を根拠にせず、原公開 metadata の九 field を再現する。"""

    if row.published_at is None:
        raise _invalid()
    return StoredDocument(
        row.document_id,
        row.project_id,
        row.folder,
        row.name,
        row.size,
        row.mime,
        row.checksum,
        row.actor_id,
        row.published_at,
    )


def _aware(value: object) -> bool:
    """原時刻の timezone を検証し、復旧時に暗黙 UTC 補正しない。"""

    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _invalid() -> DocumentUploadInvalidError:
    """内部 path・body・identity を公開例外に含めない。"""

    return DocumentUploadInvalidError("Original document effect upload could not be verified")
