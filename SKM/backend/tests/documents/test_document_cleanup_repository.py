"""清理要求が旧 upload 履歴を補造せず、元情報と現在の要求者を保存することを検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from skillmind.documents.cleanup_repository import DocumentCleanupRepository
from skillmind.documents.domain import (
    DocumentCleanupActor,
    DocumentStorageUnavailableError,
    StoredDocument,
)
from skillmind.storage import BlobReference, InMemoryFileStorage
from tests.documents.fakes import document_content, stored_document


def _facts() -> tuple[DocumentCleanupActor, StoredDocument, BlobReference]:
    """本文も接続も使わず、元文書と別の認可済み清理要求者を表す。"""

    actor = DocumentCleanupActor(uuid4(), uuid4(), uuid4(), uuid4())
    document = stored_document(uuid4(), document_content())
    reference = BlobReference(
        key=f"projects/{document.project_id}/{document.document_id}",
        namespace=InMemoryFileStorage().namespace,
    )
    return actor, document, reference


@pytest.mark.parametrize("linked", [False, True])
def test_cleanup_retains_original_metadata_storage_and_current_delete_actor(linked: bool) -> None:
    """旧文書の要求に偽の upload key/session を作らず、linked 原予約も消去しない。"""

    actor, document, reference = _facts()
    session = MagicMock()
    intent_id = uuid4() if linked else None
    DocumentCleanupRepository(session).request(
        actor=actor, document=document, reference=reference, upload_intent_id=intent_id,
    )
    row = session.add.call_args.args[0]
    assert row.document_id == document.document_id and row.project_id == document.project_id
    assert row.organization_id == actor.organization_id and row.requested_by == actor.actor_id
    assert row.request_id == actor.request_id and row.session_id == actor.session_id
    assert row.uploaded_by == document.uploaded_by and row.uploaded_by != row.requested_by
    assert row.document_created_at == document.created_at
    assert row.created_at >= row.document_created_at
    assert (row.folder, row.name, row.size, row.mime, row.checksum) == (
        document.folder, document.name, document.size, document.mime, document.checksum,
    )
    assert row.storage_key == reference.key
    assert reference.namespace is not None
    assert (row.storage_namespace_id, row.storage_descriptor_checksum, row.storage_is_durable) == (
        reference.namespace.namespace_id, reference.namespace.descriptor_checksum,
        reference.namespace.durable,
    )
    assert row.upload_intent_id == intent_id
    assert row.source_protocol == ("UPLOAD_INTENT_V1" if linked else "LEGACY_UNVERIFIED")
    assert row.protocol_version == 1
    session.delete.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize(("field", "value"), [
    ("document_id", UUID(int=0)), ("project_id", UUID(int=0)),
    ("uploaded_by", UUID(int=0)), ("size", -1), ("size", 0), ("size", True),
    ("checksum", "invalid"), ("name", ""), ("folder", "x" * 201), ("mime", ""),
    ("created_at", datetime(2026, 1, 1)),
    ("created_at", datetime.max.replace(tzinfo=UTC)),
])
def test_invalid_metadata_cannot_be_lost_during_cleanup_request(field: str, value: object) -> None:
    """不正な課金量や保存時刻を新規 row へ変換せず、目録削除前に拒否する。"""

    actor, document, reference = _facts()
    session = MagicMock()
    with pytest.raises(DocumentStorageUnavailableError):
        DocumentCleanupRepository(session).request(
            actor=actor,
            document=replace(document, **{field: value}),  # type: ignore[arg-type]
            reference=reference, upload_intent_id=None,
        )
    session.add.assert_not_called()


@pytest.mark.parametrize("field", ["organization_id", "actor_id", "request_id", "session_id"])
def test_cleanup_requires_current_non_nil_delete_identity(field: str) -> None:
    """元 upload の不明な要求者を現在の削除者の代わりに保存しない。"""

    actor, document, reference = _facts()
    session = MagicMock()
    with pytest.raises(DocumentStorageUnavailableError):
        DocumentCleanupRepository(session).request(
            actor=replace(actor, **{field: UUID(int=0)}), document=document,
            reference=reference, upload_intent_id=None,
        )
    session.add.assert_not_called()


@pytest.mark.parametrize("changed", ["unbound", "unsafe_key", "nil_intent"])
def test_cleanup_does_not_guess_unbound_storage_or_repair_unsafe_object_keys(changed: str) -> None:
    """原所属/key の欠落を新しい UUID や現在設定で補わない。"""

    actor, document, reference = _facts()
    session = MagicMock()
    if changed == "unbound":
        reference = replace(reference, namespace=None)
    elif changed == "unsafe_key":
        reference = replace(reference, key="../unrelated")
    with pytest.raises(DocumentStorageUnavailableError):
        DocumentCleanupRepository(session).request(
            actor=actor, document=document, reference=reference,
            upload_intent_id=UUID(int=0) if changed == "nil_intent" else None,
        )
    session.add.assert_not_called()
