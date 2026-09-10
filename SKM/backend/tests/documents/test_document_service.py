"""DocumentService の upload 安全境界と object storage 連携を検証する。"""

from __future__ import annotations

import pytest

from skillmind.storage import UploadRejectedError
from tests.documents.upload_harness import UPLOAD_LIMITS, UploadDatabase


async def test_upload_stores_blob_and_persists_normalized_metadata() -> None:
    """許可 upload は同一 byte を保存し、原 actor に属する正規化 metadata を公開する。"""

    db = UploadDatabase()
    stored = await db.upload(
        folder="specs/", name="overview.md", data=b"# Overview\n",
        content_type="text/markdown; charset=utf-8",
    )
    assert stored.name == "overview.md" and stored.folder == "specs"
    assert stored.mime == "text/markdown"
    assert stored.checksum.startswith("sha256:")
    assert stored.uploaded_by == db.access.actor.user_id
    document = db.documents[0]
    assert document.storage_key.startswith(f"projects/{db.project.id}/documents/")
    assert await db.blobs.get(document.storage_key) == b"# Overview\n"


async def test_upload_rejects_quota_exceeded_before_storage_write() -> None:
    """既存 usage と合算で quota を超える upload は storage 前に拒否する。"""

    db = UploadDatabase()
    db.document.size = UPLOAD_LIMITS.project_quota_bytes
    db.documents = [db.document]
    with pytest.raises(UploadRejectedError) as excinfo:
        await db.upload()
    assert excinfo.value.code == "project_quota_exceeded"
    assert db.documents == [db.document]
    db.storage.put.assert_not_awaited()


async def test_upload_rejects_credential_like_text_content() -> None:
    """合成の credential 風本文は transaction/storage に入る前に拒否する。"""

    db = UploadDatabase()
    with pytest.raises(UploadRejectedError) as excinfo:
        await db.upload(data=b"api_key = synthetic-rejected-value")
    assert excinfo.value.code == "sensitive_content"
    assert db.transactions == 0
    db.storage.put.assert_not_awaited()


async def test_upload_rejects_unsafe_document_name() -> None:
    """path segment を含む名前は単一 file 名でないため拒否する。"""

    db = UploadDatabase()
    with pytest.raises(UploadRejectedError) as excinfo:
        await db.upload(name="../escape.txt")
    assert excinfo.value.code == "invalid_document_name"
    assert db.transactions == 0
    db.storage.put.assert_not_awaited()
