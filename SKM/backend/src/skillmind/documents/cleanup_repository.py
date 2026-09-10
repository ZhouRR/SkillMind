"""新旧文書の原清理対象を目録削除と同時に保存し、byte 占用を失わせない。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import ProjectDocumentCleanup
from skillmind.documents.domain import (
    DocumentCleanupActor,
    DocumentStorageUnavailableError,
    StoredDocument,
)
from skillmind.storage import (
    BlobReference,
    FileStorageError,
    StorageNamespace,
    sanitize_object_key,
)


class DocumentCleanupRepository:
    """外部 DELETE や予約解放は行わず、原対象と要求事実だけを保持する。"""

    def __init__(self, session: AsyncSession) -> None:
        """目録と同じ transaction を共有し、独立 commit を許さない。"""

        self._session = session

    def request(
        self, *, actor: DocumentCleanupActor, document: StoredDocument,
        reference: BlobReference, upload_intent_id: UUID | None,
    ) -> None:
        """旧文書の占用も同時移管し、存在しない過去の upload key/session を作らない。"""

        now = datetime.now(UTC)
        namespace = _validate_target(actor, document, reference, upload_intent_id, now)
        self._session.add(ProjectDocumentCleanup(
            id=uuid4(), organization_id=actor.organization_id, project_id=document.project_id,
            document_id=document.document_id, requested_by=actor.actor_id,
            request_id=actor.request_id, session_id=actor.session_id,
            upload_intent_id=upload_intent_id, protocol_version=1,
            source_protocol="LEGACY_UNVERIFIED" if upload_intent_id is None else "UPLOAD_INTENT_V1",
            folder=document.folder, name=document.name, size=document.size, mime=document.mime,
            checksum=document.checksum, uploaded_by=document.uploaded_by,
            document_created_at=document.created_at, created_at=now,
            storage_key=reference.key, storage_namespace_id=namespace.namespace_id,
            storage_descriptor_checksum=namespace.descriptor_checksum,
            storage_is_durable=namespace.durable,
        ))


def _validate_target(
    actor: DocumentCleanupActor, document: StoredDocument, reference: BlobReference,
    upload_intent_id: UUID | None, now: datetime,
) -> StorageNamespace:
    """消去後に復元できない原識別子・byte 数・保存先を曖昧な値のまま引き継がない。"""

    if (
        any(not isinstance(value, UUID) or value.int == 0 for value in (
            actor.organization_id, actor.actor_id, actor.request_id, actor.session_id,
            document.project_id, document.document_id, document.uploaded_by,
        ))
        or (upload_intent_id is not None and (
            not isinstance(upload_intent_id, UUID) or upload_intent_id.int == 0
        ))
        or type(document.size) is not int or document.size <= 0
        or not isinstance(document.folder, str) or len(document.folder) > 200
        or not isinstance(document.name, str) or not 1 <= len(document.name) <= 200
        or not isinstance(document.mime, str) or not 1 <= len(document.mime) <= 128
        or not isinstance(document.checksum, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", document.checksum) is None
        or not isinstance(document.created_at, datetime)
        or document.created_at.tzinfo is None or document.created_at.utcoffset() is None
        or document.created_at > now
        or not isinstance(reference.key, str) or not 1 <= len(reference.key) <= 512
        or not isinstance(reference.namespace, StorageNamespace)
    ):
        raise _invalid()
    try:
        if sanitize_object_key(reference.key) != reference.key:
            raise _invalid()
    except FileStorageError as error:
        raise _invalid() from error
    return reference.namespace


def _invalid() -> DocumentStorageUnavailableError:
    """内部 key や壊れた metadata を公開例外の本文へ流さない。"""

    return DocumentStorageUnavailableError("Document cleanup target could not be verified")
