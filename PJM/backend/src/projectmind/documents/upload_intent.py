"""原 upload の descriptor と不変な公開回执を定義し、HTTP や SDK から分離する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.documents.domain import (
    DocumentCleanupActor,
    StoredDocumentUpload,
    UploadDocumentCommand,
)


def upload_request_checksum(
    *, organization_id: UUID, project_id: UUID, actor_id: UUID,
    folder: str, name: str, size: int, mime: str, checksum: str,
) -> str:
    """新しい server request/session/object ID を除き、正規化済み原入力だけを固定する。"""

    return f"sha256:{sha256_hex(canonical_json({
        'protocol_version': 1,
        'organization_id': str(organization_id),
        'project_id': str(project_id),
        'actor_id': str(actor_id),
        'folder': folder,
        'name': name,
        'size': size,
        'mime': mime,
        'checksum': checksum,
    }))}"


@dataclass(frozen=True, slots=True)
class StoredUploadIntent:
    """Repository が照合済みの原 writer、保存先、要求と公開結果を渡す内部 DTO。"""

    intent_id: UUID
    organization_id: UUID
    actor_id: UUID
    original_request_id: UUID
    original_session_id: UUID
    request_checksum: str
    command: UploadDocumentCommand
    receipt: StoredDocumentUpload
    cleanup_requested_at: datetime | None
    publication_closed_at: datetime | None = None


def upload_closure_checksum(
    intent: StoredUploadIntent, *, closure_id: UUID,
    actor: DocumentCleanupActor, closed_at: datetime,
) -> str:
    """v1 入力 hash を変えず、原 key/対象/受付と一回の閉鎖監査を独立して固定する。"""

    command = intent.command
    namespace = command.storage_namespace
    return f"sha256:{sha256_hex(canonical_json({
        'protocol': 'projectmind.document-upload-closure/v1',
        'upload_intent': {
            'id': str(intent.intent_id),
            'protocol_version': 1,
            'organization_id': str(intent.organization_id),
            'project_id': str(command.project_id),
            'actor_id': str(intent.actor_id),
            'upload_key': str(intent.receipt.upload_key),
            'original_request_id': str(intent.original_request_id),
            'original_session_id': str(intent.original_session_id),
            'created_at': intent.receipt.created_at.astimezone(UTC).isoformat(),
            'request_checksum': intent.request_checksum,
            'document_id': str(command.document_id),
            'folder': command.folder,
            'name': command.name,
            'size': command.size,
            'mime': command.mime,
            'checksum': command.checksum,
            'storage_key': command.storage_key,
            'storage_namespace_id': str(namespace.namespace_id),
            'storage_descriptor_checksum': namespace.descriptor_checksum,
            'storage_is_durable': namespace.durable,
            'write_protocol': 'UNCONDITIONAL_V1',
        },
        'closure': {
            'id': str(closure_id),
            'organization_id': str(actor.organization_id),
            'requested_by': str(actor.actor_id),
            'request_id': str(actor.request_id),
            'session_id': str(actor.session_id),
            'closed_at': closed_at.astimezone(UTC).isoformat(),
        },
    }))}"
