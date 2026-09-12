"""S3 の version 固定と取得競合を SDK seam で検証する。実 MinIO の証明ではない。"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from minio.error import S3Error
from urllib3.exceptions import ProtocolError

from skillmind.storage import BlobNotFoundError, BlobReadLimitExceededError, FileStorageError
from skillmind.storage.s3 import S3FileStorage
from tests.storage.test_s3_reads import _Response


def _headers(version: str | None = "original-version") -> dict[str, str]:
    """hash ではない opaque ETag と実取得時刻を持つ合成 HTTP metadata。"""

    result = {
        "last-modified": "Fri, 11 Sep 2026 01:02:03 GMT",
        "etag": '"opaque-multipart-3"',
        "content-length": "3",
        "content-type": "text/plain; charset=utf-8",
    }
    if version is not None:
        result["x-amz-version-id"] = version
    return result


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str | None = "original-version",
    data: bytes = b"abc",
) -> tuple[S3FileStorage, Mock, _Response, dict[str, str]]:
    """HTTP response の所有権 fixture を共有し、HEAD と GET を独立に差替え可能にする。"""

    response = _Response(data, chunk_size=1)
    headers = _headers(version)
    monkeypatch.setattr(response, "headers", headers, raising=False)
    client = Mock(spec=["stat_object", "get_object"])
    client.stat_object.return_value = SimpleNamespace(metadata=dict(headers))
    client.get_object.return_value = response
    storage = S3FileStorage(
        endpoint="http://storage.invalid:9000", bucket="test", access_key="test", secret_key="test"
    )
    monkeypatch.setattr(storage, "_client", client)
    return storage, client, response, headers


@pytest.mark.parametrize("version", ["original-version", None, "null"])
async def test_read_pins_immutable_version_or_compares_unversioned_metadata(
    monkeypatch: pytest.MonkeyPatch, version: str | None
) -> None:
    """null version を固定と誤認せず、HEAD 時点と同じ GET の bytes/時刻を返す。"""

    storage, client, response, _ = _setup(monkeypatch, version=version)
    result = await storage.get_observed("original/key", max_bytes=3)
    pinned = version not in {None, "null"}
    assert result.data == b"abc"
    assert result.observation.last_modified == datetime(2026, 9, 11, 1, 2, 3, tzinfo=UTC)
    assert result.observation.etag == "opaque-multipart-3"
    assert result.observation.version_id == version
    assert result.observation.version_pinned is pinned
    assert result.observation.content_type == "text/plain; charset=utf-8"
    client.get_object.assert_called_once_with(
        "test", "original/key", version_id=version if pinned else None
    )
    assert client.stat_object.call_count == (1 if pinned else 2)
    assert response.closed == response.released == 1
    assert len(set(response.threads)) == 1 and threading.get_ident() not in response.threads
    assert "abc" not in repr(result)


@pytest.mark.parametrize("stage", ["get", "after"])
@pytest.mark.parametrize(
    "header,value",
    [
        ("etag", '"replaced"'),
        ("last-modified", "Fri, 11 Sep 2026 01:02:04 GMT"),
        ("content-length", "4"),
        ("content-type", "application/octet-stream"),
        ("x-amz-version-id", "another-version"),
    ],
)
async def test_changed_object_is_rejected_without_refetch(
    monkeypatch: pytest.MonkeyPatch, stage: str, header: str, value: str
) -> None:
    """GET 中・取得後の各観測差を失敗に保ち、別 version に読み替えない。"""

    storage, client, response, headers = _setup(monkeypatch, version=None)
    if stage == "get":
        headers[header] = value
    else:
        changed = {**headers, header: value}
        client.stat_object.side_effect = [
            SimpleNamespace(metadata=dict(headers)),
            SimpleNamespace(metadata=changed),
        ]
    with pytest.raises(FileStorageError, match="changed during acquisition"):
        await storage.get_observed("original/key", max_bytes=3)
    assert client.get_object.call_count == 1
    assert response.closed == response.released == 1


@pytest.mark.parametrize("stage", ["head", "get"])
@pytest.mark.parametrize(
    "header,value",
    [
        ("last-modified", None),
        ("last-modified", "not a date"),
        ("last-modified", "Fri, 11 Sep 2026 01:02:03"),
        ("etag", None),
        ("etag", 'W/"weak"'),
        ("etag", "unquoted"),
        ("content-length", None),
        ("content-length", "-1"),
        ("content-type", None),
        ("x-amz-version-id", ""),
    ],
)
async def test_incomplete_observation_cannot_be_fabricated_or_downgraded(
    monkeypatch: pytest.MonkeyPatch, stage: str, header: str, value: str | None
) -> None:
    """必須 header の欠落や不正は失敗し、通常 GET でやり直さない。"""

    storage, client, response, headers = _setup(monkeypatch)
    malformed = dict(headers)
    if value is None:
        malformed.pop(header, None)
    else:
        malformed[header] = value
    if stage == "head":
        client.stat_object.return_value = SimpleNamespace(metadata=malformed)
    else:
        headers.clear()
        headers.update(malformed)
    with pytest.raises(FileStorageError, match="observation is invalid"):
        await storage.get_observed("original/key", max_bytes=3)
    assert client.get_object.call_count == (0 if stage == "head" else 1)
    assert response.closed == response.released == (0 if stage == "head" else 1)


@pytest.mark.parametrize(
    "data,limit,error",
    [
        (b"ab", 3, FileStorageError),
        (b"abcd", 3, BlobReadLimitExceededError),
        (b"abc", 2, BlobReadLimitExceededError),
    ],
)
async def test_actual_body_must_match_metadata_and_bound(
    monkeypatch: pytest.MonkeyPatch, data: bytes, limit: int, error: type[Exception]
) -> None:
    """短い正文や虚偽 length を返さず、HEAD 超過では GET 自体を始めない。"""

    storage, client, response, _ = _setup(monkeypatch, data=data)
    with pytest.raises(error):
        await storage.get_observed("original/key", max_bytes=limit)
    assert client.get_object.call_count == (0 if limit < 3 else 1)
    assert response.closed == response.released == (0 if limit < 3 else 1)


@pytest.mark.parametrize("code", ["NoSuchVersion", "NoSuchKey", "NoSuchBucket", "AccessDenied"])
async def test_lost_version_and_unavailable_storage_remain_distinct(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """旧 version の削除後は最新へ切り替えず、bucket/権限失敗を不存在としない。"""

    storage, client, _, _ = _setup(monkeypatch)
    client.get_object.side_effect = S3Error(
        response=Mock(),
        code=code,
        message="private",
        resource="private",
        request_id="r",
        host_id="h",
    )
    with pytest.raises(FileStorageError) as caught:
        await storage.get_observed("original/key", max_bytes=3)
    assert isinstance(caught.value, BlobNotFoundError) is (code in {"NoSuchVersion", "NoSuchKey"})
    assert "private" not in str(caught.value)
    assert client.get_object.call_count == 1


async def test_partial_read_failure_releases_without_returning_partial_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metadata 一致でも本文取得失敗は成功にならず、接続を返却する。"""

    storage, _, response, _ = _setup(monkeypatch)
    response.read_error = ProtocolError("private")
    response.close_error = OSError("private")
    with pytest.raises(FileStorageError, match="storage is unavailable"):
        await storage.get_observed("original/key", max_bytes=3)
    assert response.closed == response.released == 1


async def test_cancelled_observed_read_keeps_thread_cleanup_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消後に SDK thread が戻っても、応答を公開せず自身で後片付けする。"""

    storage, _, response, _ = _setup(monkeypatch)
    response.gate = threading.Event()
    task = asyncio.create_task(storage.get_observed("original/key", max_bytes=3))
    try:
        assert await asyncio.to_thread(response.entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        response.gate.set()
        assert await asyncio.to_thread(response.released_event.wait, 2)
    assert response.closed == response.released == 1


@pytest.mark.parametrize("version", ["original-version", None, "null"])
async def test_inspection_does_not_download_and_later_read_uses_original_observation(
    monkeypatch: pytest.MonkeyPatch,
    version: str | None,
) -> None:
    """選択時は HEAD のみ、後の取得はその観測を維持して元 bytes を返す。"""

    storage, client, response, _ = _setup(monkeypatch, version=version)
    observed = await storage.inspect("original/key")
    client.get_object.assert_not_called()
    assert not response.calls and response.closed == response.released == 0
    result = await storage.get_observed("original/key", max_bytes=3, expected=observed)
    assert result.observation == observed and result.data == b"abc"
    assert client.stat_object.call_count == (1 if observed.version_pinned else 3)
    assert response.closed == response.released == 1


async def test_latest_version_replacement_does_not_replace_inspected_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """選択後に現行 version が変わっても、旧 version 指定の GET だけを行う。"""

    storage, client, _, headers = _setup(monkeypatch)
    observed = await storage.inspect("original/key")
    client.stat_object.return_value = SimpleNamespace(
        metadata={**headers, "x-amz-version-id": "new-version", "etag": '"new-content"'}
    )
    result = await storage.get_observed("original/key", max_bytes=3, expected=observed)
    assert result.data == b"abc" and result.observation == observed
    client.get_object.assert_called_once_with("test", "original/key", version_id="original-version")
    assert client.stat_object.call_count == 1


@pytest.mark.parametrize("version", [None, "null"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("etag", '"new-content"'),
        ("last-modified", "Fri, 11 Sep 2026 01:02:04 GMT"),
        ("x-amz-version-id", "new-version"),
    ],
)
async def test_unversioned_change_since_selection_fails_before_download(
    monkeypatch: pytest.MonkeyPatch,
    version: str | None,
    field: str,
    value: str,
) -> None:
    """選択と取得の間の変更を検出し、現在の値で選択結果を上書きしない。"""

    storage, client, response, headers = _setup(monkeypatch, version=version)
    observed = await storage.inspect("original/key")
    client.stat_object.return_value = SimpleNamespace(metadata={**headers, field: value})
    with pytest.raises(FileStorageError, match="changed since inspection"):
        await storage.get_observed("original/key", max_bytes=3, expected=observed)
    client.get_object.assert_not_called()
    assert response.closed == response.released == 0


@pytest.mark.parametrize("code", ["NoSuchKey", "NoSuchBucket", "AccessDenied", "SlowDown"])
async def test_inspection_does_not_treat_unavailability_as_absence(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    """metadata だけの読取にも通常取得と同じ不存在分類を適用する。"""

    storage, client, _, _ = _setup(monkeypatch)
    client.stat_object.side_effect = S3Error(
        response=Mock(),
        code=code,
        message="private",
        resource="private",
        request_id="r",
        host_id="h",
    )
    with pytest.raises(FileStorageError) as caught:
        await storage.inspect("original/key")
    assert isinstance(caught.value, BlobNotFoundError) is (code == "NoSuchKey")
    assert "private" not in str(caught.value)
    client.get_object.assert_not_called()
