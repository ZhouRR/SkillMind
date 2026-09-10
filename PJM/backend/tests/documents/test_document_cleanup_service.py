"""新旧目録の削除を独立清理要求へ移し、失敗/取消で原 byte 占用を失わないことを検証する。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from projectmind.auth.sessions import UnauthorizedSessionError
from projectmind.documents.domain import DocumentNotFoundError, DocumentStorageUnavailableError
from projectmind.documents.service import DocumentService
from projectmind.storage import FileStorageError, UploadLimits, UploadRejectedError
from tests.documents.fakes import document_content
from tests.documents.test_document_upload_authorization import expire
from tests.documents.test_document_upload_intents import _restart, _usage
from tests.documents.upload_harness import UploadDatabase


async def _legacy_database() -> UploadDatabase:
    """既知の memory namespace に属する旧行を合成し、upload intent は補造しない。"""

    db = UploadDatabase()
    document = db.document
    namespace = db.blobs.namespace
    document.storage_namespace_id = namespace.namespace_id
    document.storage_descriptor_checksum = namespace.descriptor_checksum
    document.storage_is_durable = namespace.durable
    document.upload_intent_id = None
    db.documents = [document]
    await db.blobs.put(document.storage_key, document_content().data, content_type=document.mime)
    return db


@pytest.mark.parametrize("failure", ["none", "storage", "commit-kept", "commit-lost", "expiry"])
async def test_legacy_delete_moves_charge_and_original_identity_to_independent_cleanup(
    failure: str,
) -> None:
    """旧 bound 文書も消去前に占用を移し、blob 503 や未知 commit で無料の孤立物を作らない。"""

    db = await _legacy_database()
    original = db.document
    assert await _usage(db) == original.size
    request_id, session_id = db.access.request_id, db.auth_session.id
    if failure == "storage":
        db.storage.delete.side_effect = FileStorageError("Synthetic DELETE unavailable")
        expected: type[BaseException] = FileStorageError
    elif failure.startswith("commit-"):
        db.commit_error, db.commit_persists = True, failure == "commit-kept"
        expected = ConnectionError
    elif failure == "expiry":
        db.on_flush = lambda: expire(db)
        expected = UnauthorizedSessionError
    if failure == "none":
        await db.remove_document()
    else:
        with pytest.raises(expected):
            await db.remove_document()
    committed = failure in {"none", "storage", "commit-kept"}
    assert bool(db.documents) is not committed
    assert len(db.cleanups) == int(committed) and not db.intents
    if committed:
        cleanup = db.cleanups[0]
        assert cleanup.source_protocol == "LEGACY_UNVERIFIED" and cleanup.upload_intent_id is None
        assert cleanup.organization_id == db.user.organization_id
        assert cleanup.project_id == original.project_id and cleanup.document_id == original.id
        assert cleanup.requested_by == db.user.id
        assert cleanup.request_id == request_id and cleanup.session_id == session_id
        assert cleanup.uploaded_by == original.uploaded_by
        assert cleanup.document_created_at == original.created_at
        assert cleanup.created_at >= cleanup.document_created_at
        for field in (
            "folder", "name", "size", "mime", "checksum", "storage_key", "storage_namespace_id",
            "storage_descriptor_checksum", "storage_is_durable",
        ):
            assert getattr(cleanup, field) == getattr(original, field)
    if failure in {"none", "storage"}:
        db.storage.delete.assert_awaited_once_with(original.storage_key)
    else:
        db.storage.delete.assert_not_awaited()
    if failure == "expiry":
        db.auth_session.idle_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    _restart(db)
    assert await _usage(db) == original.size
    assert await db.blobs.exists(original.storage_key) is (failure != "none")
    db.storage.put.assert_not_awaited()
    if committed:
        deletes = db.storage.delete.await_count
        with pytest.raises(DocumentNotFoundError):
            await db.remove_document()
        assert len(db.cleanups) == 1 and db.storage.delete.await_count == deletes


async def test_legacy_cleanup_survives_service_restart_and_blocks_new_quota_spend() -> None:
    """旧目録が 404 になっても清理占用は復帰し、次の upload が空 quota と解釈しない。"""

    db = await _legacy_database()
    db.storage.delete.side_effect = FileStorageError("Synthetic DELETE acknowledgement unavailable")
    with pytest.raises(FileStorageError):
        await db.remove_document()
    _restart(db)
    db.document_service = DocumentService(
        db.session_factory, file_storage=db.storage,
        limits=UploadLimits(1000, db.document.size, frozenset({"text/plain"})),
    )
    with pytest.raises(UploadRejectedError) as caught:
        await db.upload(name="new.txt")
    assert caught.value.code == "project_quota_exceeded"
    assert len(db.cleanups) == 1 and not db.documents and not db.intents
    assert await _usage(db) == db.document.size
    assert await db.blobs.exists(db.document.storage_key)
    db.storage.put.assert_not_awaited()


async def test_cancellation_while_legacy_delete_commit_waits_keeps_metadata_charge() -> None:
    """取消された未 commit 清理は目録と一緒に巻戻し、元 blob は一度も削除しない。"""

    db = await _legacy_database()
    db.commit_release = asyncio.Event()
    task = asyncio.create_task(db.remove_document())
    try:
        await asyncio.wait_for(db.commit_entered.wait(), 2)
        assert len(db.cleanups) == 1 and not db.documents
        db.storage.delete.assert_not_awaited()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _restart(db)
    assert db.documents == [db.document] and not db.cleanups and not db.intents
    assert await _usage(db) == db.document.size
    assert await db.blobs.exists(db.document.storage_key)
    db.storage.delete.assert_not_awaited()


async def test_linked_cleanup_is_audit_only_and_does_not_double_original_intent_charge() -> None:
    """新文書にも独立要求を保存するが、課金は元 intent に残して清理行を二重加算しない。"""

    db = UploadDatabase()
    document = await db.upload()
    db.document = db.documents[0]
    await db.remove_document()
    assert len(db.cleanups) == len(db.intents) == 1 and not db.documents
    cleanup, intent = db.cleanups[0], db.intents[0]
    assert cleanup.upload_intent_id == intent.id
    assert cleanup.source_protocol == "UPLOAD_INTENT_V1"
    assert cleanup.document_id == intent.document_id == document.document_id
    assert cleanup.size == intent.size == document.size
    assert intent.cleanup_requested_at is not None
    assert await _usage(db) == document.size
    db.storage.delete.assert_awaited_once_with(intent.storage_key)


async def test_linked_cleanup_and_original_marker_both_rollback_after_final_auth_expiry() -> None:
    """最終認可失効では独立要求・元 marker・目録の三つを同一 rollback に戻す。"""

    db = UploadDatabase()
    document = await db.upload()
    db.document = db.documents[0]
    db.on_flush = lambda: expire(db)
    with pytest.raises(UnauthorizedSessionError):
        await db.remove_document()
    assert db.documents == [db.document] and not db.cleanups
    assert db.intents[0].cleanup_requested_at is None and db.intents[0].state == "PUBLISHED"
    db.auth_session.idle_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    _restart(db)
    assert await _usage(db) == document.size
    db.storage.delete.assert_not_awaited()


async def test_invalid_legacy_size_is_not_removed_or_transferred_as_zero_charge() -> None:
    """破損した旧負数を正規化して削除せず、metadata と元 storage を未変更で保持する。"""

    db = await _legacy_database()
    db.document.size = -1
    with pytest.raises(DocumentStorageUnavailableError):
        await db.remove_document()
    assert db.documents == [db.document] and not db.cleanups and not db.intents
    assert db.rollbacks == 1
    db.storage.delete.assert_not_awaited()
