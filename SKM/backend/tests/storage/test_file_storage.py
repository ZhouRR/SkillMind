"""FileStorage 抽象、in-memory 実装、アップロード検証、機密走査を検証する。"""

from __future__ import annotations

import pytest

from skillmind.core.redaction import contains_sensitive_content
from skillmind.storage import (
    BlobNotFoundError,
    FileStorageError,
    InMemoryFileStorage,
    UploadLimits,
    UploadRejectedError,
    sanitize_object_key,
)
from skillmind.storage.s3 import S3FileStorage


async def test_in_memory_round_trip_reports_size_and_sha256() -> None:
    """put→get→stat が正文・size・sha256 を一貫して返す。"""

    storage = InMemoryFileStorage()
    blob = await storage.put("projects/p1/documents/a.txt", b"hi there", content_type="text/plain")

    assert blob.key == "projects/p1/documents/a.txt"
    assert blob.size == 8
    assert blob.content_type == "text/plain"
    assert blob.sha256.startswith("sha256:")
    assert await storage.get("projects/p1/documents/a.txt") == b"hi there"
    assert await storage.exists("projects/p1/documents/a.txt") is True
    assert await storage.exists("projects/p1/documents/missing.txt") is False
    stat = await storage.stat("projects/p1/documents/a.txt")
    assert stat == blob


async def test_in_memory_delete_and_missing_raise_not_found() -> None:
    """削除後の get と未登録 key の get/stat が BlobNotFoundError を送出する。"""

    storage = InMemoryFileStorage()
    await storage.put("projects/p1/a.txt", b"x", content_type="text/plain")
    await storage.delete("projects/p1/a.txt")
    await storage.delete("projects/p1/a.txt")  # 冪等: 無くてもエラーにしない。

    with pytest.raises(BlobNotFoundError):
        await storage.get("projects/p1/a.txt")
    with pytest.raises(BlobNotFoundError):
        await storage.stat("projects/p1/unknown.txt")


@pytest.mark.parametrize(
    "key",
    ["/etc/passwd", "projects/../secret", "a/b/", "a\\b", "", "a/\x01/b", "."],
)
def test_sanitize_object_key_rejects_unsafe(key: str) -> None:
    """絶対・dot・trailing slash・backslash・制御文字・空 key を拒否する。"""

    with pytest.raises(FileStorageError):
        sanitize_object_key(key)


def test_sanitize_object_key_accepts_relative_posix() -> None:
    """安全な相対 POSIX key はそのまま返す。"""

    assert sanitize_object_key("projects/p1/documents/d1.txt") == "projects/p1/documents/d1.txt"


def test_upload_limits_accept_clean_upload_and_normalize_content_type() -> None:
    """許可された text は content-type を正規化して受理する。"""

    limits = UploadLimits(
        max_bytes=100,
        project_quota_bytes=200,
        allowed_content_types=frozenset({"text/plain", "application/pdf"}),
    )

    normalized = limits.validate(
        size=11,
        content_type="Text/Plain; charset=utf-8",
        content=b"hello world",
        project_usage_bytes=0,
    )
    assert normalized == "text/plain"


@pytest.mark.parametrize(
    ("size", "content_type", "content", "usage", "code"),
    [
        (0, "text/plain", b"", 0, "empty_upload"),
        (101, "text/plain", b"x" * 101, 0, "file_too_large"),
        (100, "text/plain", b"x" * 100, 150, "project_quota_exceeded"),
        (10, "application/x-sh", b"echo hi", 0, "content_type_not_allowed"),
        (17, "text/plain", b"password: hunter2", 0, "sensitive_content"),
    ],
)
def test_upload_limits_reject_with_stable_code(
    size: int, content_type: str, content: bytes, usage: int, code: str
) -> None:
    """size/配額/種別/機密の違反を安定 code で fail-closed に拒否する。"""

    limits = UploadLimits(
        max_bytes=100,
        project_quota_bytes=200,
        allowed_content_types=frozenset({"text/plain", "application/pdf"}),
    )

    with pytest.raises(UploadRejectedError) as excinfo:
        limits.validate(
            size=size, content_type=content_type, content=content, project_usage_bytes=usage
        )
    assert excinfo.value.code == code


def test_contains_sensitive_content_detects_credentials_and_private_keys() -> None:
    """credential 代入と private key marker を検出し、通常本文は素通しする。"""

    assert contains_sensitive_content("api_key = ABC123") is True
    assert contains_sensitive_content("-----BEGIN RSA PRIVATE KEY-----") is True
    assert contains_sensitive_content("# Rules\nUse evidence before judgement.") is False


def test_s3_storage_constructs_from_endpoint_without_connecting() -> None:
    """MinIO client は遅延接続のため、endpoint から構築できることだけ確認する。"""

    storage = S3FileStorage(
        endpoint="http://object-storage:9000",
        bucket="skillmind",
        access_key="skillmind",
        secret_key="secret",
    )
    assert isinstance(storage, S3FileStorage)
