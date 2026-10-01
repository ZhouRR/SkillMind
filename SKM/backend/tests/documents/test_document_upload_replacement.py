"""画面 upload の同名更新を、原 intent・本文保持・競合・未知結果の境界で回帰する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from skillmind.auth.sessions import UnauthorizedSessionError
from skillmind.documents.domain import (
    DocumentConflictError,
    DocumentNotFoundError,
    DocumentUploadKeyConflictError,
    DocumentUploadPendingError,
)
from skillmind.documents.repository import DocumentRepository
from skillmind.storage import FileStorageError, UploadRejectedError
from tests.documents.test_document_upload_authorization import expire
from tests.documents.test_document_upload_intents import _lookup, _restart, _usage
from tests.documents.upload_harness import UploadDatabase


async def update(db: UploadDatabase, previous, *, upload_key: UUID | None = None):
    """画面が選択時に固定した元 ID/path/hash を実 service に渡す。"""
    return await db.document_service.upload_document(
        project_id=db.project.id, upload_key=upload_key or uuid4(), access=db.access,
        folder=previous.folder, name=previous.name, data=b"updated", content_type="text/plain",
        replaces_document_id=previous.document_id, expected_checksum=previous.checksum,
    )


async def test_update_keeps_old_bytes_and_original_receipts_without_delete_first():
    """新 ID の公開だけを切り替え、原 byte・過去の upload 回执・quota を維持する。"""
    db = UploadDatabase()
    old = await db.upload()
    old_key = db.last_upload_key
    old_row = db.documents[0]
    old_storage_key = old_row.storage_key
    key = uuid4()
    new = await update(db, old, upload_key=key)
    _restart(db)
    assert await update(db, old, upload_key=key) == new
    assert old_row.id == old.document_id and old_row.checksum == old.checksum
    assert old_row.storage_key == old_storage_key and old_row.deleted_at is not None
    assert new.document_id != old.document_id
    assert [row.id for row in db.documents if row.deleted_at is None] == [new.document_id]
    assert (await _lookup(db, old_key)).document == old
    assert (await _lookup(db, key)).document == new
    async with db.transaction():
        original, reference = await DocumentRepository(db.session).get_for_download(
            project_id=db.project.id, document_id=old.document_id,
        )
    assert original == old
    assert await db.blobs.get(reference.key) == b"hello"
    assert await _usage(db) == 12
    assert db.storage.put.await_count == 2
    db.storage.delete.assert_not_called()


@pytest.mark.parametrize("change", ["rename", "trash", "hash", "id", "project"])
async def test_stale_selection_is_refused_before_put(change):
    """元 path/ID/版が変わった場合、現在の同名文書を探して上書きしない。"""
    db = UploadDatabase()
    old = await db.upload()
    row = db.documents[0]
    if change == "rename":
        row.name = "renamed.txt"
    elif change == "trash":
        row.deleted_at = datetime.now(UTC)
    elif change == "hash":
        row.checksum = "sha256:" + "a" * 64
    elif change == "id":
        row.id = uuid4()
    else:
        old = replace(old, document_id=uuid4())
    error = DocumentNotFoundError if change in {"id", "project"} else DocumentConflictError
    with pytest.raises(error):
        await update(db, old)
    assert db.storage.put.await_count == 1 and len(db.intents) == 1


async def test_publication_conflict_closes_only_new_upload_and_allows_fresh_update():
    """PUT 中の改名は旧文書を保存し、確認済み競合で path の未決占有を残さない。"""
    db = UploadDatabase()
    old = await db.upload()
    db.on_put = lambda: setattr(db.documents[0], "name", "renamed.txt")
    key = uuid4()
    with pytest.raises(DocumentConflictError):
        await update(db, old, upload_key=key)
    assert len(db.documents) == 1 and db.documents[0].deleted_at is None
    assert db.intents[-1].publication_closed_at is not None and len(db.closures) == 1
    assert (await _lookup(db, key)).state == "PENDING"
    db.on_put = None
    new = await update(db, replace(old, name="renamed.txt"))
    assert new.name == "renamed.txt"
    db.storage.delete.assert_not_called()


async def test_unknown_put_preserves_old_version_and_cannot_be_replayed():
    """storage 応答を失っても旧版を回収せず、同 key で PUT を繰り返さない。"""
    db = UploadDatabase()
    old = await db.upload()
    db.storage.put.side_effect = FileStorageError("Synthetic acknowledgement loss")
    key = uuid4()
    with pytest.raises(FileStorageError):
        await update(db, old, upload_key=key)
    _restart(db)
    with pytest.raises(DocumentUploadPendingError):
        await update(db, old, upload_key=key)
    assert len(db.documents) == 1 and db.documents[0].deleted_at is None
    assert db.storage.put.await_count == 2


async def test_revoked_access_during_put_cannot_replace_original():
    """元会話の失効は PUT 後にも検査し、旧文書の回収や新版の公開を許さない。"""
    db = UploadDatabase()
    old = await db.upload()
    db.on_put = lambda: expire(db)
    with pytest.raises(UnauthorizedSessionError):
        await update(db, old)
    assert len(db.documents) == 1 and db.documents[0].deleted_at is None
    assert db.intents[-1].state == "PENDING" and not db.closures


async def test_replacement_target_is_part_of_original_request_identity():
    """同じ本文でも元 hash を変更した要求は、原 upload として再採用しない。"""
    db = UploadDatabase()
    old = await db.upload()
    key = uuid4()
    await update(db, old, upload_key=key)
    with pytest.raises(DocumentUploadKeyConflictError):
        await update(db, replace(old, checksum="sha256:" + "b" * 64), upload_key=key)
    assert db.storage.put.await_count == 2


async def test_failed_publication_transaction_rolls_back_old_version():
    """公開 flush が失敗すると旧版を可視状態に戻し、原 PENDING から再送しない。"""
    db = UploadDatabase()
    old = await db.upload()

    def fail_flush():
        """新予約の commit 後、元行の回収を含む公開 transaction だけに障害を入れる。"""
        raise RuntimeError("Synthetic publication failure")

    db.on_put = lambda: setattr(db.session.flush, "side_effect", fail_flush)
    with pytest.raises(RuntimeError, match="Synthetic"):
        await update(db, old)
    assert len(db.documents) == 1 and db.documents[0].deleted_at is None
    assert db.intents[-1].state == "PENDING"


async def test_update_keeps_storage_quota_for_referenced_original():
    """旧 byte を保持する更新は旧容量を減算せず、上限超過なら PUT 前に拒否する。"""
    from skillmind.documents.service import DocumentService
    from tests.documents.upload_harness import UPLOAD_LIMITS

    db = UploadDatabase()
    old = await db.upload()
    db.document_service = DocumentService(
        db.session_factory, file_storage=db.storage,
        limits=replace(UPLOAD_LIMITS, project_quota_bytes=10),
    )
    with pytest.raises(UploadRejectedError):
        await update(db, old)
    assert db.documents[0].deleted_at is None and db.storage.put.await_count == 1
