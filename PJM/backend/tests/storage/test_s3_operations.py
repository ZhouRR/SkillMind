"""実 bucket を使わず、S3 操作の成功・不存在・拒否・取消を区別する。"""

from __future__ import annotations

import asyncio
import io
import threading
from types import SimpleNamespace
from typing import Literal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from minio.error import InvalidResponseError, MinioException, S3Error
from urllib3.exceptions import HTTPError, ProtocolError

from projectmind.core.hashing import sha256_hex
from projectmind.storage import BlobNotFoundError, FileStorageError
from projectmind.storage.blob import StoredBlob
from projectmind.storage.namespace import make_s3_namespace
from projectmind.storage.s3 import S3FileStorage

_Operation = Literal["put", "delete", "exists", "stat"]
_OPERATIONS: tuple[_Operation, ...] = ("put", "delete", "exists", "stat")
_METHODS = {"put": "put_object", "delete": "remove_object", "exists": "stat_object",
            "stat": "stat_object"}
_KEY = "original/private-key"
_DATA = b"binary\x00content\xff"
_DIGEST = f"sha256:{sha256_hex(_DATA)}"


def _storage(monkeypatch: pytest.MonkeyPatch) -> tuple[S3FileStorage, Mock]:
    """SDK 構築自体を差し替え、設定 file・credential provider・network を使わない。"""

    client = Mock(spec=["put_object", "remove_object", "stat_object", "get_object"])
    client.stat_object.return_value = SimpleNamespace(
        size=len(_DATA), content_type="application/octet-stream",
        metadata={"x-amz-meta-sha256": _DIGEST},
    )
    monkeypatch.setattr("projectmind.storage.s3.Minio", Mock(return_value=client))
    return S3FileStorage(
        endpoint="http://storage.invalid:9000", bucket="test", access_key="test", secret_key="test"
    ), client


def _s3_error(code: str) -> S3Error:
    """固定の合成内部詳細を付け、adapter の公開 message へ混ざらないことを検査する。"""

    return S3Error(
        response=Mock(), code=code, message="private SDK detail", resource="private resource",
        request_id="private request", host_id="private host",
    )


async def _invoke(storage: S3FileStorage, operation: _Operation) -> StoredBlob | bool | None:
    """同じ元 key を使い、操作間の例外分類と余分な SDK 呼出しを比較する。"""

    if operation == "put":
        return await storage.put(_KEY, _DATA, content_type="application/octet-stream")
    if operation == "delete":
        await storage.delete(_KEY)
        return None
    if operation == "exists":
        return await storage.exists(_KEY)
    return await storage.stat(_KEY)


def _assert_single_call(client: Mock, operation: _Operation) -> None:
    """再送・確認 GET/stat・補償 DELETE を adapter が追加しないことを守る。"""

    assert len(client.method_calls) == 1
    method = getattr(client, _METHODS[operation])
    method.assert_called_once()
    assert method.call_args.args[:2] == ("test", _KEY)
    client.get_object.assert_not_called()


@pytest.mark.parametrize(
    "endpoint,authority,secure", [
        ("http://STORAGE.INVALID:80/", "storage.invalid", False),
        ("https://STORAGE.INVALID:443/", "storage.invalid", True),
        ("http://storage.invalid:9000", "storage.invalid:9000", False),
        ("https://storage.invalid:9443", "storage.invalid:9443", True),
        ("https://[2001:0db8:0:0::1]:443/", "[2001:db8::1]", True),
        ("http://[2001:db8::1]:9000", "[2001:db8::1]:9000", False),
    ],
)
def test_sdk_endpoint_and_frozen_namespace_use_same_canonical_target(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, authority: str, secure: bool,
) -> None:
    """SDK の実宛先と保存 identity を一致させ、scheme/port/IPv6 と鍵 rotation を守る。"""

    client = Mock(spec=["put_object", "remove_object", "stat_object", "get_object"])
    constructor = Mock(return_value=client)
    monkeypatch.setattr("projectmind.storage.s3.Minio", constructor)
    namespace_id = uuid4()
    storage = S3FileStorage(
        endpoint=endpoint, bucket="test", access_key="first", secret_key="first",
        namespace_id=namespace_id,
    )
    constructor.assert_called_once_with(
        authority, access_key="first", secret_key="first", secure=secure
    )
    canonical = f"{'https' if secure else 'http'}://{authority}"
    assert storage.namespace == make_s3_namespace(
        namespace_id=namespace_id, endpoint=canonical, bucket="test"
    )
    rotated = S3FileStorage(
        endpoint=canonical, bucket="test", access_key="rotated", secret_key="rotated",
        namespace_id=namespace_id,
    )
    assert rotated.namespace == storage.namespace
    assert not client.method_calls


@pytest.mark.parametrize(
    "endpoint", ["http://user:password@storage.invalid", "http://storage.invalid/path",
                 "http://storage.invalid?key=value", "http://storage.invalid#fragment"],
)
def test_ambiguous_endpoint_is_rejected_before_sdk_construction(
    monkeypatch: pytest.MonkeyPatch, endpoint: str,
) -> None:
    """SDK が捨てる宛先成分や URL credential を client 構築前に静的拒否する。"""

    constructor = Mock(side_effect=AssertionError("Invalid endpoint must not reach SDK"))
    monkeypatch.setattr("projectmind.storage.s3.Minio", constructor)
    with pytest.raises(FileStorageError, match=r"^Object storage endpoint is invalid$"):
        S3FileStorage(
            endpoint=endpoint, bucket="test", access_key="test", secret_key="test",
            namespace_id=uuid4(),
        )
    constructor.assert_not_called()


def test_unconfigured_s3_namespace_stays_explicitly_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """従来の SDK 呼出契約を保つ一方、未設定 identity を勝手に作らない。"""

    storage, client = _storage(monkeypatch)
    assert storage.namespace is None
    assert not client.method_calls


@pytest.mark.parametrize("operation", _OPERATIONS)
async def test_s3_operation_success_preserves_original_contract(
    monkeypatch: pytest.MonkeyPatch, operation: _Operation,
) -> None:
    """成功値と原 key を維持し、PUT では同じ本文・size・hash を SDK に渡す。"""

    storage, client = _storage(monkeypatch)
    result = await _invoke(storage, operation)
    if operation in {"put", "stat"}:
        assert result == StoredBlob(
            key=_KEY, size=len(_DATA), content_type="application/octet-stream", sha256=_DIGEST
        )
    elif operation == "exists":
        assert result is True
    else:
        assert result is None
    _assert_single_call(client, operation)
    if operation == "put":
        stream, size = client.put_object.call_args.args[2:]
        assert isinstance(stream, io.BytesIO) and stream.getvalue() == _DATA
        assert size == len(_DATA)
        assert client.put_object.call_args.kwargs == {
            "content_type": "application/octet-stream", "metadata": {"sha256": _DIGEST},
        }


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize(
    "code", ["NoSuchKey", "NoSuchObject", "NoSuchBucket", "AccessDenied", "SlowDown",
             "InternalError", "NoSuchVersion", "nosuchkey"],
)
async def test_s3_operations_only_classify_exact_object_missing_codes(
    monkeypatch: pytest.MonkeyPatch, operation: _Operation, code: str,
) -> None:
    """bucket/資格/未知 error を不存在にせず、PUT の失敗を未保存とも解釈しない。"""

    storage, client = _storage(monkeypatch)
    getattr(client, _METHODS[operation]).side_effect = _s3_error(code)
    missing = code in {"NoSuchKey", "NoSuchObject"}
    if missing and operation == "delete":
        assert await _invoke(storage, operation) is None
    elif missing and operation == "exists":
        assert await _invoke(storage, operation) is False
    else:
        with pytest.raises(FileStorageError) as caught:
            await _invoke(storage, operation)
        if missing and operation == "stat":
            assert type(caught.value) is BlobNotFoundError
            assert str(caught.value) == "Blob content is not available"
        else:
            assert type(caught.value) is FileStorageError
            assert str(caught.value) == "Blob storage is unavailable"
    _assert_single_call(client, operation)


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize(
    "error", [MinioException("private SDK detail"), InvalidResponseError(503, "x", "private"),
              HTTPError("private HTTP detail"), ProtocolError("private disconnect"),
              OSError("private IO detail"), TimeoutError("private timeout")],
)
async def test_s3_operation_transport_failures_are_static_unavailable(
    monkeypatch: pytest.MonkeyPatch, operation: _Operation, error: Exception,
) -> None:
    """SDK・HTTP・OS の通信失敗を同じ静的 error にし、存否や書込結果を捏造しない。"""

    storage, client = _storage(monkeypatch)
    getattr(client, _METHODS[operation]).side_effect = error
    with pytest.raises(FileStorageError) as caught:
        await _invoke(storage, operation)
    assert type(caught.value) is FileStorageError
    assert str(caught.value) == "Blob storage is unavailable"
    _assert_single_call(client, operation)


async def test_put_response_failure_never_compensates_an_applied_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """遠端保存後の応答喪失でも、未保存として成功扱い・再送・削除をしない。"""

    storage, client = _storage(monkeypatch)
    applied: list[bytes] = []

    def put(*args: object, **kwargs: object) -> None:
        """受けた元本文の保存直後に通信失敗する SDK を模倣する。"""

        stream = args[2]
        assert isinstance(stream, io.BytesIO)
        applied.append(stream.read())
        raise ProtocolError("private response loss")

    client.put_object.side_effect = put
    with pytest.raises(FileStorageError, match=r"^Blob storage is unavailable$"):
        await _invoke(storage, "put")
    assert applied == [_DATA]
    _assert_single_call(client, "put")
    client.remove_object.assert_not_called()


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("late_failure", [False, True])
async def test_cancellation_does_not_claim_sdk_stopped_or_add_compensation(
    monkeypatch: pytest.MonkeyPatch, operation: _Operation, late_failure: bool,
) -> None:
    """取消をそのまま伝え、継続する SDK thread の遅い結果を成功や補償へ変換しない。"""

    storage, client = _storage(monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    applied: list[_Operation] = []
    threads: list[int] = []

    def execute(*args: object, **kwargs: object) -> object:
        """loop の取消後まで thread を保持し、終了を test 側で必ず待てるようにする。"""

        try:
            threads.append(threading.get_ident())
            entered.set()
            assert release.wait(3), "The event loop must remain available"
            applied.append(operation)
            if late_failure:
                raise ProtocolError("private late SDK response loss")
            return client.stat_object.return_value
        finally:
            finished.set()

    getattr(client, _METHODS[operation]).side_effect = execute
    task = asyncio.create_task(_invoke(storage, operation))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not applied and not finished.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert applied == [operation]
    assert threads and threading.get_ident() not in threads
    _assert_single_call(client, operation)
