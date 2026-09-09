"""実 bucket を使わず、S3 の同一有界読取・障害分類・thread 所有権を検証する。"""

from __future__ import annotations

import asyncio
import io
import threading
from unittest.mock import Mock

import pytest
from minio.error import InvalidResponseError, S3Error
from urllib3.exceptions import ProtocolError

from projectmind.storage import (
    BlobNotFoundError,
    BlobReadLimitExceededError,
    FileStorageError,
    InMemoryFileStorage,
)
from projectmind.storage.s3 import S3FileStorage


class _Response:
    """HTTPResponse の read(amount) と接続後片付けを模倣する。"""

    def __init__(self, data: bytes, *, chunk_size: int | None = None) -> None:
        """一度に読める量と実際に消費した byte を記録する。"""

        self.stream = io.BytesIO(data)
        self.chunk_size = chunk_size
        self.calls: list[int | None] = []
        self.threads: list[int] = []
        self.closed = 0
        self.released = 0
        self.read_error: Exception | None = None
        self.close_error: Exception | None = None
        self.entered = threading.Event()
        self.released_event = threading.Event()
        self.gate: threading.Event | None = None

    def read(self, amount: int | None = None, *, decode_content: bool) -> bytes:
        """申告 Content-Length に頼らず、要求量以下の同じ byte stream を返す。"""

        assert decode_content is False
        self.calls.append(amount)
        self.threads.append(threading.get_ident())
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(3), "The event loop must be free to release the read"
        if self.read_error is not None and len(self.calls) > 1:
            raise self.read_error
        if amount is None:
            return self.stream.read()
        return self.stream.read(min(amount, self.chunk_size or amount))

    def close(self) -> None:
        """read 失敗後も同じ thread で close が呼ばれたことを記録する。"""

        self.threads.append(threading.get_ident())
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error

    def release_conn(self) -> None:
        """close 失敗後も接続の返却が試行されることを記録する。"""

        self.threads.append(threading.get_ident())
        self.released += 1
        self.released_event.set()


def _storage(monkeypatch: pytest.MonkeyPatch, response: _Response) -> tuple[S3FileStorage, Mock]:
    """実 client を接続前に置き換え、GET 以外の呼出しは持たない。"""

    storage = S3FileStorage(
        endpoint="http://storage.invalid:9000", bucket="test", access_key="test", secret_key="test"
    )
    client = Mock(spec=["get_object"])
    client.get_object.return_value = response
    monkeypatch.setattr(storage, "_client", client)
    return storage, client


@pytest.mark.parametrize(
    "data,limit,chunk_size,exceeded",
    [
        (b"", 0, None, False),
        (b"a", 0, None, True),
        (b"abc", 3, None, False),
        (b"abc", 5, 1, False),
        (b"abcde", 3, 1, True),
        (b"x" * 300_000, 70_000, None, True),
        (b"x" * 131_072, 131_072, None, False),
        (b"abc", None, None, False),
    ],
)
async def test_s3_reads_same_stream_with_exact_limit_and_thread_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    limit: int | None,
    chunk_size: int | None,
    exceeded: bool,
) -> None:
    """空・境界・短 chunk・大正文を切詰めず、上限 + 1 byte だけで判定する。"""

    response = _Response(data, chunk_size=chunk_size)
    storage, client = _storage(monkeypatch, response)
    if exceeded:
        with pytest.raises(BlobReadLimitExceededError):
            await storage.get("original/key", max_bytes=limit)
    else:
        assert await storage.get("original/key", max_bytes=limit) == data
    client.get_object.assert_called_once_with("test", "original/key")
    assert response.stream.tell() == (limit + 1 if exceeded and limit is not None else len(data))
    assert response.closed == response.released == 1
    assert len(set(response.threads)) == 1 and threading.get_ident() not in response.threads
    if limit is not None:
        assert all(amount is not None and 0 < amount <= 65_536 for amount in response.calls)


@pytest.mark.parametrize(
    "code", ["NoSuchKey", "NoSuchObject", "AccessDenied", "NoSuchBucket", "SlowDown"]
)
async def test_only_exact_s3_object_missing_codes_mean_absent(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """資格・bucket・一時拒否を元文書の不存在と誤認しない。"""

    response = _Response(b"")
    storage, client = _storage(monkeypatch, response)
    client.get_object.side_effect = S3Error(
        response=Mock(),
        code=code,
        message="private detail",
        resource="private key",
        request_id="r",
        host_id="h",
    )
    with pytest.raises(FileStorageError) as caught:
        await storage.get("original/key", max_bytes=3)
    assert isinstance(caught.value, BlobNotFoundError) is (code in {"NoSuchKey", "NoSuchObject"})
    assert "private" not in str(caught.value) and "original" not in str(caught.value)
    assert response.closed == response.released == 0


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("private timeout"),
        ProtocolError("private transport"),
        InvalidResponseError(503, "x", "private"),
    ],
)
async def test_get_transport_errors_are_unavailable_not_missing(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """接続成立前の HTTP/SDK 失敗も内部本文を出さない同じ境界へ畳む。"""

    storage, client = _storage(monkeypatch, _Response(b""))
    client.get_object.side_effect = error
    with pytest.raises(FileStorageError) as caught:
        await storage.get("original/key", max_bytes=3)
    assert not isinstance(caught.value, BlobNotFoundError)
    assert str(caught.value) == "Blob storage is unavailable"


@pytest.mark.parametrize("close_fails", [False, True])
async def test_partial_read_and_close_failures_still_release_response(
    monkeypatch: pytest.MonkeyPatch, close_fails: bool
) -> None:
    """途中の read と close の両失敗でも release を飛ばさず、部分正文を返さない。"""

    response = _Response(b"abcdef", chunk_size=2)
    response.read_error = ProtocolError("private read error")
    response.close_error = OSError("private close error") if close_fails else None
    storage, _ = _storage(monkeypatch, response)
    with pytest.raises(FileStorageError, match="storage is unavailable"):
        await storage.get("original/key", max_bytes=6)
    assert response.stream.tell() == 2
    assert response.closed == response.released == 1


async def test_cancelled_await_does_not_abandon_thread_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消は遠端停止と同一視せず、遅い read が戻った thread が必ず後片付けする。"""

    response = _Response(b"abc")
    response.gate = threading.Event()
    storage, _ = _storage(monkeypatch, response)
    task = asyncio.create_task(storage.get("original/key", max_bytes=3))
    try:
        assert await asyncio.to_thread(response.entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert response.closed == response.released == 0
    finally:
        response.gate.set()
        assert await asyncio.to_thread(response.released_event.wait, 2)
    assert response.closed == response.released == 1


@pytest.mark.parametrize(
    "size,limit,exceeded", [(0, 0, False), (1, 0, True), (3, 3, False), (4, 3, True)]
)
async def test_memory_storage_implements_identical_bound(
    size: int, limit: int, exceeded: bool
) -> None:
    """offline backend でも超過正文を返さず、本番と同じ port を検証する。"""

    storage = InMemoryFileStorage()
    await storage.put("original/key", b"x" * size, content_type="text/plain")
    if exceeded:
        with pytest.raises(BlobReadLimitExceededError):
            await storage.get("original/key", max_bytes=limit)
    else:
        assert await storage.get("original/key", max_bytes=limit) == b"x" * size


@pytest.mark.parametrize("limit", [-1, True])
async def test_invalid_read_limit_is_rejected_before_sdk(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    """不正な上限を無制限取得に変換しない。"""

    storage, client = _storage(monkeypatch, _Response(b"x"))
    for backend in (storage, InMemoryFileStorage()):
        with pytest.raises(FileStorageError, match="non-negative integer"):
            await backend.get("original/key", max_bytes=limit)
    client.get_object.assert_not_called()
