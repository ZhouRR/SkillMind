"""内部保存先の固定と fail-closed を実 service/repository と合成 DB で検証する。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from skillmind.documents.domain import DocumentStorageUnavailableError
from skillmind.documents.repository import DocumentRepository
from skillmind.documents.service import DocumentService
from skillmind.documents.snapshot import DocumentSnapshotError
from skillmind.documents.source import DatabaseProjectDocumentSource, read_frozen_document
from skillmind.storage import BlobReference, InMemoryFileStorage, StorageNamespace
from tests.documents.deletion_harness import DeletionDatabase
from tests.documents.fakes import document_content, document_snapshot
from tests.documents.upload_harness import UPLOAD_LIMITS, UploadDatabase

_PUBLIC_FIELDS = {
    "document_id",
    "project_id",
    "folder",
    "name",
    "size",
    "mime",
    "checksum",
    "uploaded_by",
    "created_at",
}


def _bind_document(db: DeletionDatabase, namespace: StorageNamespace) -> None:
    """合成 row の原保存先を明示し、現在 client からの自動補完に頼らない。"""

    db.document.storage_namespace_id = namespace.namespace_id
    db.document.storage_descriptor_checksum = namespace.descriptor_checksum
    db.document.storage_is_durable = namespace.durable


async def _assert_reads_rejected(db: DeletionDatabase) -> None:
    """通常 download と凍結 source が同じ拒否を返し、内部 locator を公開しない。"""

    with pytest.raises(DocumentStorageUnavailableError) as caught:
        await db.document_service.download_document(
            project_id=db.project.id, document_id=db.document.id
        )
    assert db.document.storage_key not in str(caught.value)
    assert str(db.document.storage_namespace_id) not in str(caught.value)
    source = DatabaseProjectDocumentSource(db.session_factory, file_storage=db.storage)
    frozen = document_snapshot(db.project.id, [document_content()]).documents[0]
    with pytest.raises(DocumentSnapshotError, match="unavailable"):
        await read_frozen_document(source, project_id=db.project.id, document=frozen)
    assert db.transactions == 0


async def _assert_delete_rejected(db: DeletionDatabase) -> None:
    """所属不明は metadata 削除前に rollback し、commit 後の blob 処理へ進まない。"""

    with pytest.raises(DocumentStorageUnavailableError):
        await db.remove_document()
    assert db.documents == [db.document]
    assert db.commits == 0 and db.rollbacks == 1
    db.session.delete.assert_not_awaited()
    db.storage.delete.assert_not_awaited()


async def test_replacement_memory_store_with_identical_key_and_bytes_is_not_original() -> None:
    """同じ key/hash を再現した別 instance は原保存先ではなく、読取も削除も許可しない。"""

    db = DeletionDatabase()
    original, replacement = InMemoryFileStorage(), InMemoryFileStorage()
    assert original.namespace != replacement.namespace
    assert original.namespace.durable is replacement.namespace.durable is False
    _bind_document(db, original.namespace)
    content = document_content()
    for storage in (original, replacement):
        await storage.put(db.document.storage_key, content.data, content_type=content.mime)
    db.storage.namespace = replacement.namespace
    db.storage.get = AsyncMock(wraps=replacement.get)
    db.storage.delete = AsyncMock(wraps=replacement.delete)

    await _assert_reads_rejected(db)
    await _assert_delete_rejected(db)

    db.storage.get.assert_not_awaited()
    for storage in (original, replacement):
        assert await storage.get(db.document.storage_key) == content.data


@pytest.mark.parametrize("field", ["namespace_id", "descriptor_checksum", "durable"])
async def test_every_namespace_component_is_required_for_document_io(field: str) -> None:
    """UUID のみ、descriptor のみの一致を、保存先全体の一致として扱わない。"""

    db = DeletionDatabase()
    original = db.storage.namespace
    if field == "namespace_id":
        replacement = replace(original, namespace_id=uuid4())
    elif field == "descriptor_checksum":
        replacement = replace(original, descriptor_checksum="sha256:" + "f" * 64)
    else:
        replacement = replace(original, durable=not original.durable)
    db.storage.namespace = replacement
    db.storage.get = AsyncMock(side_effect=AssertionError("No GET for another namespace"))

    await _assert_reads_rejected(db)
    await _assert_delete_rejected(db)

    db.storage.get.assert_not_awaited()
    assert db.storage.namespace == replacement


@pytest.mark.parametrize(
    "changes",
    [
        {
            "storage_namespace_id": None,
            "storage_descriptor_checksum": None,
            "storage_is_durable": None,
        },
        {"storage_namespace_id": None},
        {"storage_descriptor_checksum": None},
        {"storage_is_durable": None},
        {"storage_namespace_id": UUID(int=0)},
        {"storage_namespace_id": "00000000-0000-4000-8000-000000000001"},
        {"storage_descriptor_checksum": "invalid"},
        {"storage_descriptor_checksum": "sha256:" + "A" * 64},
        {"storage_is_durable": 0},
        {"storage_is_durable": "false"},
    ],
    ids=[
        "legacy-unbound",
        "missing-id",
        "missing-descriptor",
        "missing-durability",
        "zero-id",
        "string-id",
        "invalid-descriptor",
        "uppercase-descriptor",
        "numeric-durability",
        "string-durability",
    ],
)
async def test_unbound_or_corrupt_ownership_preserves_metadata_without_blob_io(
    changes: dict[str, object],
) -> None:
    """旧行や部分破損は metadata 閲覧を保つが、現在設定で修復せず byte 操作を拒否する。"""

    db = DeletionDatabase()
    for field, value in changes.items():
        setattr(db.document, field, value)
    db.storage.get = AsyncMock(side_effect=AssertionError("No GET without original ownership"))

    metadata = await db.read_document()
    assert set(asdict(metadata)) == _PUBLIC_FIELDS
    await _assert_reads_rejected(db)
    await _assert_delete_rejected(db)

    db.storage.get.assert_not_awaited()
    for field, value in changes.items():
        assert getattr(db.document, field) == value


async def test_namespace_changed_during_get_never_returns_even_identical_bytes() -> None:
    """GET 中の client 切替は完了後にも検出し、検証済み hash だけで読取を成功させない。"""

    db = DeletionDatabase()
    original = db.storage.namespace

    async def get_then_switch(key: str, *, max_bytes: int) -> bytes:
        """元 key の完全な byte を返す直前に、次の client 世代への切替を注入する。"""

        assert key == db.document.storage_key and max_bytes == db.document.size
        db.storage.namespace = InMemoryFileStorage().namespace
        return document_content().data

    db.storage.get = AsyncMock(side_effect=get_then_switch)
    with pytest.raises(DocumentStorageUnavailableError):
        await db.document_service.download_document(
            project_id=db.project.id, document_id=db.document.id
        )
    db.storage.get.assert_awaited_once()
    assert db.document.storage_namespace_id == original.namespace_id
    assert db.documents == [db.document] and db.transactions == 0


async def test_upload_persists_original_namespace_without_expanding_public_metadata() -> None:
    """新規保存は実 client の三要素を行へ固定し、公開九 field に内部保存先を漏らさない。"""

    db = UploadDatabase()
    original = db.blobs.namespace
    stored = await db.upload()
    document = db.documents[0]

    assert (
        document.storage_namespace_id,
        document.storage_descriptor_checksum,
        document.storage_is_durable,
    ) == (original.namespace_id, original.descriptor_checksum, original.durable)
    assert set(asdict(stored)) == _PUBLIC_FIELDS
    metadata, reference = await DocumentRepository(db.session).get_for_download(
        project_id=db.project.id, document_id=stored.document_id
    )
    assert metadata == stored
    assert reference == BlobReference(document.storage_key, original)
    assert db.storage.namespace == original
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("boundary", ["before-put", "during-put", "before-publish"])
async def test_upload_namespace_switch_never_publishes_or_compensates(boundary: str) -> None:
    """初回 namespace を await 後に再選択せず、PUT 済み byte も推測で削除しない。"""

    db = UploadDatabase()
    original = db.blobs.namespace
    replacement = InMemoryFileStorage().namespace

    def switch_namespace() -> None:
        """対象の外部待機点だけで client の保存先を別世代へ変更する。"""

        db.storage.namespace = replacement

    def switch_after_usage() -> None:
        """quota 待機中に切替し、PUT 前と公開前の検査を別々に通過させる。"""

        expected_read = 1 if boundary == "before-put" else 2
        if db.usage_reads == expected_read:
            switch_namespace()

    if boundary == "during-put":
        db.on_put = switch_namespace
    else:
        db.on_usage = switch_after_usage

    with pytest.raises(DocumentStorageUnavailableError):
        await db.upload()

    assert not db.documents and db.commits == 1
    assert db.storage.namespace == replacement and db.blobs.namespace == original
    assert db.transactions == (2 if boundary == "before-publish" else 1)
    assert db.rollbacks == int(boundary == "before-publish")
    assert db.storage.put.await_count == int(boundary != "before-put")
    db.storage.delete.assert_not_awaited()
    if boundary != "before-put":
        assert await db.blobs.get(db.storage.put.await_args.args[0]) == b"hello"


async def test_upload_without_configured_namespace_never_puts_or_publishes() -> None:
    """未設定 adapter を新規文書の暗黙保存先にせず、認証後も byte I/O 前で止める。"""

    db = UploadDatabase()
    db.storage.namespace = None
    with pytest.raises(DocumentStorageUnavailableError):
        await db.upload()
    assert not db.documents
    db.storage.put.assert_not_awaited()
    db.storage.delete.assert_not_awaited()


async def test_actual_memory_adapter_round_trip_preserves_persisted_namespace() -> None:
    """属性 mock だけでなく実 adapter の PUT→DB→通常/frozen source GET を結ぶ。"""

    db = UploadDatabase()
    storage = InMemoryFileStorage()
    db.document_service = DocumentService(
        db.session_factory, file_storage=storage, limits=UPLOAD_LIMITS,
    )
    stored = await db.upload()
    assert db.documents[0].storage_namespace_id == storage.namespace.namespace_id
    assert await db.document_service.download_document(
        project_id=db.project.id, document_id=stored.document_id,
    ) == (stored, b"hello")
    content = await DatabaseProjectDocumentSource(
        db.session_factory, file_storage=storage,
    ).fetch(project_id=db.project.id, document_id=stored.document_id)
    assert content is not None and content.data == b"hello"
    assert content.document_id == stored.document_id and content.checksum == stored.checksum


async def test_namespace_switch_during_delete_commit_does_not_touch_replacement_storage() -> None:
    """DB commit 後も原保存先を確認し、既に metadata が消えても別 client を削除しない。"""

    db = DeletionDatabase()
    original = db.storage.namespace
    db.commit_release = asyncio.Event()
    task = asyncio.create_task(db.remove_document())
    try:
        await asyncio.wait_for(db.commit_entered.wait(), 2)
        db.storage.delete.assert_not_awaited()
        db.storage.namespace = InMemoryFileStorage().namespace
        db.commit_release.set()
        with pytest.raises(DocumentStorageUnavailableError):
            await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not db.documents and db.commits == 1 and db.rollbacks == 0
    assert db.document.storage_namespace_id == original.namespace_id
    db.storage.delete.assert_not_awaited()
