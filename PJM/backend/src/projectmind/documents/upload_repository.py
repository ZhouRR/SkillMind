"""原 upload とその byte 占用を、文書目録と独立した持続行として保持する。"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.db.models import ProjectDocumentUpload, ProjectDocumentUploadClosure
from projectmind.documents.domain import (
    DocumentCleanupActor,
    DocumentUploadAlreadyPublishedError,
    DocumentUploadClosedError,
    DocumentUploadInvalidError,
    StoredDocument,
    StoredDocumentUpload,
    StoredDocumentUploadClosure,
    UploadDocumentCommand,
)
from projectmind.documents.paths import document_storage_key, validate_document_path
from projectmind.documents.upload_intent import (
    StoredUploadIntent,
    upload_closure_checksum,
    upload_request_checksum,
)
from projectmind.storage import (
    BlobReference,
    FileStorageError,
    StorageNamespace,
    UploadRejectedError,
)
from projectmind.storage.validation import normalize_content_type

_CHECKSUM = re.compile(r"sha256:[0-9a-f]{64}")


class DocumentUploadRepository:
    """Organization 認可 gate の内側で予約・公開・清理要求の原事実を保存する。"""

    def __init__(self, session: AsyncSession) -> None:
        """呼出元の短い認可 transaction を共有し、独立 commit や storage I/O をしない。"""

        self._session = session

    async def find(
        self, *, organization_id: UUID, project_id: UUID, actor_id: UUID, upload_key: UUID,
    ) -> StoredUploadIntent | None:
        """原 actor/Project/key のみ锁定し、別 actor の同じ key を公開しない。"""

        row = await self._session.scalar(
            select(ProjectDocumentUpload).where(
                ProjectDocumentUpload.organization_id == organization_id,
                ProjectDocumentUpload.project_id == project_id,
                ProjectDocumentUpload.actor_id == actor_id,
                ProjectDocumentUpload.upload_key == upload_key,
            ).with_for_update().execution_options(populate_existing=True)
        )
        if row is None:
            return None
        result = _to_intent(row)
        # NULL marker と孤立監査の不一致も、未停止と推測して再公開しない。
        await self.get_closure(result)
        return result

    async def path_reserved(self, *, project_id: UUID, folder: str, name: str) -> bool:
        """未公開原要求が占有する展示 path を、次の PUT より前に保護する。"""

        return bool(await self._session.scalar(select(exists().where(
            ProjectDocumentUpload.project_id == project_id,
            ProjectDocumentUpload.folder == folder,
            ProjectDocumentUpload.name == name,
            ProjectDocumentUpload.state == "PENDING",
            ProjectDocumentUpload.publication_closed_at.is_(None),
        ))))

    def reserve(
        self, *, upload_key: UUID, organization_id: UUID,
        original_request_id: UUID, original_session_id: UUID,
        command: UploadDocumentCommand, now: datetime,
    ) -> StoredUploadIntent:
        """commit 済み予約だけが一度の application PUT を許し、再受付は再 PUT しない。"""

        namespace = command.storage_namespace
        row = ProjectDocumentUpload(
            id=command.upload_intent_id,
            organization_id=organization_id,
            project_id=command.project_id,
            actor_id=command.uploaded_by,
            upload_key=upload_key,
            original_request_id=original_request_id,
            original_session_id=original_session_id,
            protocol_version=1,
            request_checksum=_command_checksum(organization_id, command),
            document_id=command.document_id,
            folder=command.folder,
            name=command.name,
            storage_key=command.storage_key,
            storage_namespace_id=namespace.namespace_id,
            storage_descriptor_checksum=namespace.descriptor_checksum,
            storage_is_durable=namespace.durable,
            write_protocol="UNCONDITIONAL_V1",
            size=command.size,
            mime=command.mime,
            checksum=command.checksum,
            state="PENDING",
            created_at=now,
            published_at=None,
            cleanup_requested_at=None,
            publication_closed_at=None,
        )
        result = _to_intent(row)
        self._session.add(row)
        return result

    async def publish(self, intent: StoredUploadIntent, document: StoredDocument) -> None:
        """原予約の同じ metadata だけを同 transaction で公開し、占用を二重に加算しない。"""

        row = await self._session.get(ProjectDocumentUpload, intent.intent_id)
        if row is None:
            raise _invalid()
        current = _to_intent(row)
        await self.get_closure(current)
        if current.publication_closed_at is not None:
            raise DocumentUploadClosedError("Original document upload publication is closed")
        if current != intent or intent.receipt.state != "PENDING":
            raise _invalid()
        if document != _published_document(intent.command, document.created_at):
            raise _invalid()
        row.state = "PUBLISHED"
        row.published_at = document.created_at
        _to_intent(row)

    async def get_closure(self, intent: StoredUploadIntent) -> StoredDocumentUploadClosure | None:
        """元意図の lock 内で停止 marker と完全な監査を対にして検証する。"""

        row = await self._session.scalar(
            select(ProjectDocumentUploadClosure).where(
                ProjectDocumentUploadClosure.upload_intent_id == intent.intent_id,
            ).execution_options(populate_existing=True)
        )
        if row is None:
            if intent.publication_closed_at is not None:
                raise _invalid()
            return None
        return _to_closure(row, intent)

    async def close_publication(
        self, intent: StoredUploadIntent, *, actor: DocumentCleanupActor, now: datetime,
    ) -> tuple[StoredDocumentUploadClosure, bool]:
        """原 PENDING を一度だけ閉じ、原占用や外部 object には一切触れない。"""

        if (
            any(not isinstance(value, UUID) or value.int == 0 for value in (
                actor.organization_id, actor.actor_id, actor.request_id, actor.session_id,
            ))
            or actor.organization_id != intent.organization_id or actor.actor_id != intent.actor_id
            or not _aware(now) or now < intent.receipt.created_at
        ):
            raise _invalid()
        # find が取得した Organization → Upload lock を最後の commit まで呼出元が保持する。
        row = await self._session.get(ProjectDocumentUpload, intent.intent_id)
        if row is None:
            raise _invalid()
        current = _to_intent(row)
        if replace(current, publication_closed_at=intent.publication_closed_at) != intent:
            raise _invalid()
        previous = await self.get_closure(current)
        if current.receipt.state == "PUBLISHED":
            raise DocumentUploadAlreadyPublishedError(
                "Original document upload is already published",
            )
        if previous is not None:
            return previous, False
        closed_at = now.astimezone(UTC)
        closure_id = uuid4()
        closure = ProjectDocumentUploadClosure(
            id=closure_id, upload_intent_id=current.intent_id,
            organization_id=current.organization_id, project_id=current.command.project_id,
            actor_id=current.actor_id, upload_key=current.receipt.upload_key,
            document_id=current.command.document_id, protocol_version=1,
            binding_checksum=upload_closure_checksum(
                current, closure_id=closure_id, actor=actor, closed_at=closed_at,
            ),
            requested_by=actor.actor_id, request_id=actor.request_id, session_id=actor.session_id,
            closed_at=closed_at,
        )
        row.publication_closed_at = closed_at
        result = _to_closure(closure, _to_intent(row))
        self._session.add(closure)
        return result, True

    async def request_cleanup(
        self, *, intent_id: UUID, document: StoredDocument, reference: BlobReference, now: datetime,
    ) -> None:
        """文書削除と同時に原対象を保持し、204/存否観察だけでは占用を解放しない。"""

        row = await self._session.scalar(
            select(ProjectDocumentUpload).where(ProjectDocumentUpload.id == intent_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if row is None:
            raise _invalid()
        intent = _to_intent(row)
        await self.get_closure(intent)
        if (
            intent.receipt.state != "PUBLISHED"
            or intent.receipt.document != document
            or reference != BlobReference(
                intent.command.storage_key, intent.command.storage_namespace,
            )
        ):
            raise _invalid()
        row.cleanup_requested_at = row.cleanup_requested_at or now
        _to_intent(row)


def _invalid() -> DocumentUploadInvalidError:
    """保存された値や内部 key を公開例外へ含めない。"""

    return DocumentUploadInvalidError("Original document upload could not be verified")


def _aware(value: object) -> bool:
    """DB の時刻を補正せず、原時刻として比較できる型だけを受け入れる。"""

    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _to_intent(row: ProjectDocumentUpload) -> StoredUploadIntent:
    """DB CHECK に加えて原 descriptor と公開 metadata の完全性を検証する。"""

    if (
        any(not isinstance(value, UUID) or value.int == 0 for value in (
            row.id, row.organization_id, row.project_id, row.actor_id, row.upload_key,
            row.original_request_id, row.original_session_id, row.document_id,
        ))
        or type(row.protocol_version) is not int or row.protocol_version != 1
        or row.write_protocol != "UNCONDITIONAL_V1"
        or row.state not in {"PENDING", "PUBLISHED"}
        or type(row.size) is not int or row.size <= 0
        or not isinstance(row.folder, str) or len(row.folder) > 200
        or not isinstance(row.name, str) or not 1 <= len(row.name) <= 200
        or not isinstance(row.storage_key, str) or not 1 <= len(row.storage_key) <= 512
        or not isinstance(row.mime, str) or not 1 <= len(row.mime) <= 128
        or not isinstance(row.checksum, str) or _CHECKSUM.fullmatch(row.checksum) is None
        or not _aware(row.created_at)
        or (row.publication_closed_at is not None and (
            not _aware(row.publication_closed_at) or row.publication_closed_at < row.created_at
            or row.state != "PENDING" or row.published_at is not None
        ))
    ):
        raise _invalid()
    try:
        if validate_document_path(
            project_id=row.project_id, folder=row.folder, name=row.name,
        ) != (row.folder, row.name) or row.storage_key != document_storage_key(
            row.project_id, row.document_id, row.name,
        ) or normalize_content_type(row.mime) != row.mime:
            raise _invalid()
        namespace = StorageNamespace(
            row.storage_namespace_id, row.storage_descriptor_checksum, row.storage_is_durable,
        )
    except (FileStorageError, UploadRejectedError) as error:
        raise _invalid() from error
    command = UploadDocumentCommand(
        project_id=row.project_id, document_id=row.document_id, folder=row.folder,
        name=row.name, storage_key=row.storage_key, storage_namespace=namespace,
        upload_intent_id=row.id, size=row.size, mime=row.mime, checksum=row.checksum,
        uploaded_by=row.actor_id,
    )
    if row.request_checksum != _command_checksum(row.organization_id, command):
        raise _invalid()
    document = None
    if row.state == "PENDING":
        if row.published_at is not None or row.cleanup_requested_at is not None:
            raise _invalid()
    else:
        if (
            row.published_at is None or not _aware(row.published_at)
            or row.published_at < row.created_at
            or (row.cleanup_requested_at is not None and (
                not _aware(row.cleanup_requested_at) or row.cleanup_requested_at < row.published_at
            ))
        ):
            raise _invalid()
        document = _published_document(command, row.published_at)
    state: Literal["PENDING", "PUBLISHED"] = (
        "PENDING" if row.state == "PENDING" else "PUBLISHED"
    )
    return StoredUploadIntent(
        intent_id=row.id, organization_id=row.organization_id, actor_id=row.actor_id,
        original_request_id=row.original_request_id, original_session_id=row.original_session_id,
        request_checksum=row.request_checksum, command=command,
        receipt=StoredDocumentUpload(
            upload_key=row.upload_key, project_id=row.project_id, state=state,
            created_at=row.created_at, document=document,
        ),
        cleanup_requested_at=row.cleanup_requested_at,
        publication_closed_at=row.publication_closed_at,
    )


def _to_closure(
    row: ProjectDocumentUploadClosure, intent: StoredUploadIntent,
) -> StoredDocumentUploadClosure:
    """公開停止の五 field は原対象と今回監査の完全照合後にだけ投影する。"""

    if (
        not isinstance(row, ProjectDocumentUploadClosure)
        or any(not isinstance(value, UUID) or value.int == 0 for value in (
            row.id, row.upload_intent_id, row.organization_id, row.project_id, row.actor_id,
            row.upload_key, row.document_id, row.requested_by, row.request_id, row.session_id,
        ))
        or type(row.protocol_version) is not int or row.protocol_version != 1
        or row.upload_intent_id != intent.intent_id
        or row.organization_id != intent.organization_id or row.actor_id != intent.actor_id
        or row.requested_by != intent.actor_id or row.project_id != intent.command.project_id
        or row.upload_key != intent.receipt.upload_key
        or row.document_id != intent.command.document_id
        or not _aware(row.closed_at) or row.closed_at != intent.publication_closed_at
        or row.closed_at < intent.receipt.created_at or intent.receipt.state != "PENDING"
        or intent.receipt.document is not None or intent.cleanup_requested_at is not None
        or not isinstance(row.binding_checksum, str)
        or _CHECKSUM.fullmatch(row.binding_checksum) is None
    ):
        raise _invalid()
    actor = DocumentCleanupActor(
        organization_id=row.organization_id, actor_id=row.requested_by,
        request_id=row.request_id, session_id=row.session_id,
    )
    if row.binding_checksum != upload_closure_checksum(
        intent, closure_id=row.id, actor=actor, closed_at=row.closed_at,
    ):
        raise _invalid()
    return StoredDocumentUploadClosure(
        upload_key=row.upload_key, project_id=row.project_id, document_id=row.document_id,
        closed_at=row.closed_at,
    )


def _command_checksum(organization_id: UUID, command: UploadDocumentCommand) -> str:
    """HTTP 受信と持続記録の核対で同じ descriptor 算法を使う。"""

    return upload_request_checksum(
        organization_id=organization_id, project_id=command.project_id,
        actor_id=command.uploaded_by, folder=command.folder, name=command.name,
        size=command.size, mime=command.mime, checksum=command.checksum,
    )


def _published_document(command: UploadDocumentCommand, created_at: datetime) -> StoredDocument:
    """消去済み目録を再作成せず、原公開時の九 field だけを再現する。"""

    return StoredDocument(
        document_id=command.document_id, project_id=command.project_id,
        folder=command.folder, name=command.name, size=command.size, mime=command.mime,
        checksum=command.checksum, uploaded_by=command.uploaded_by, created_at=created_at,
    )
