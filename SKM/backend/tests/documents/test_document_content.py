"""通常 download と凍結 source が元 ID と同じ byte 検証を維持することを検証する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Self, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from skillmind.documents.domain import (
    DocumentContentInvalidError,
    DocumentContentMissingError,
    DocumentNotFoundError,
    DocumentStorageUnavailableError,
    StoredDocument,
)
from skillmind.documents.repository import DocumentRepository
from skillmind.documents.service import DocumentService
from skillmind.documents.snapshot import DocumentSnapshotError
from skillmind.documents.source import DatabaseProjectDocumentSource, read_frozen_document
from skillmind.storage import BlobReference, FileStorageError, InMemoryFileStorage, UploadLimits
from skillmind.storage.observation import BlobObservation, ObservedBlob
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.documents.fakes import document_content, document_snapshot, stored_document

_ORIGINAL_KEY = "original/identity"


class _Session:
    """読取専用 use case の context だけを表し、SQL は repository seam で確認する。"""

    async def __aenter__(self) -> Self:
        """DB 接続や transaction を作らない。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """元の例外を抑制しない。"""


def _session_factory() -> _Session:
    """読取ごとに独立した最小 context を返す。"""

    return _Session()


def _readers(storage: InMemoryFileStorage) -> tuple[DocumentService, DatabaseProjectDocumentSource]:
    """同じ storage と metadata 解決 seam を使う本番の二つの入口を作る。"""

    factory = cast(async_sessionmaker[AsyncSession], _session_factory)
    return (
        DocumentService(
            factory,
            file_storage=storage,
            limits=UploadLimits(100, 200, frozenset({"text/plain"})),
        ),
        DatabaseProjectDocumentSource(factory, file_storage=storage),
    )


def _metadata(
    monkeypatch: pytest.MonkeyPatch,
    document: StoredDocument | None,
    storage: InMemoryFileStorage,
) -> AsyncMock:
    """元 key/namespace と metadata を返し、repository の呼出し identity を検証する。"""

    lookup = AsyncMock(
        return_value=(document, BlobReference(_ORIGINAL_KEY, storage.namespace)),
        side_effect=DocumentNotFoundError("private metadata") if document is None else None,
    )
    monkeypatch.setattr(DocumentRepository, "get_for_download", lookup)
    return lookup


@pytest.mark.parametrize("data", [b"", b"plain body", "中文文档".encode()])
async def test_download_and_frozen_source_return_the_same_verified_bytes(
    monkeypatch: pytest.MonkeyPatch, data: bytes
) -> None:
    """空・Unicode の byte を metadata 通りに取得し、stat や同名再探索を行わない。"""

    project_id = uuid4()
    content = document_content(data)
    document = stored_document(project_id, content)
    storage = InMemoryFileStorage()
    lookup = _metadata(monkeypatch, document, storage)
    await storage.put(_ORIGINAL_KEY, data, content_type=content.mime)
    get = AsyncMock(wraps=storage.get)
    stat = AsyncMock(side_effect=AssertionError("No stat-before-get"))
    monkeypatch.setattr(storage, "get", get)
    monkeypatch.setattr(storage, "stat", stat)
    service, source = _readers(storage)
    assert await service.download_document(
        project_id=project_id, document_id=document.document_id
    ) == (document, data)
    assert (
        await read_frozen_document(
            source,
            project_id=project_id,
            document=document_snapshot(project_id, [content]).documents[0],
        )
        == replace(content, source_object_key=_ORIGINAL_KEY)
    )
    assert lookup.await_count == get.await_count == 2
    for call in lookup.await_args_list:
        assert call.kwargs == {"project_id": project_id, "document_id": document.document_id}
    for call in get.await_args_list:
        assert call.args == (_ORIGINAL_KEY,) and call.kwargs == {"max_bytes": len(data)}
    stat.assert_not_called()


@pytest.mark.parametrize(
    "case,error_type",
    [
        ("missing", DocumentContentMissingError),
        ("short", DocumentContentInvalidError),
        ("long", DocumentContentInvalidError),
        ("same_size", DocumentContentInvalidError),
        ("storage", DocumentStorageUnavailableError),
    ],
)
async def test_download_and_frozen_source_fail_without_repairing_original_facts(
    monkeypatch: pytest.MonkeyPatch, case: str, error_type: type[Exception]
) -> None:
    """不足・超過・同 size の改変・存否不明を区別し、凍結側は従来の失敗型に閉じる。"""

    project_id = uuid4()
    content = document_content(b"original")
    document = stored_document(project_id, content)
    frozen = document_snapshot(project_id, [content]).documents[0]
    storage = InMemoryFileStorage()
    _metadata(monkeypatch, document, storage)
    if case not in {"missing", "storage"}:
        actual = {"short": b"orig", "long": b"original extra", "same_size": b"modified"}[case]
        await storage.put(_ORIGINAL_KEY, actual, content_type=content.mime)
    if case == "storage":
        monkeypatch.setattr(storage, "get", AsyncMock(side_effect=FileStorageError("private key")))
    service, source = _readers(storage)
    with pytest.raises(error_type) as caught:
        await service.download_document(project_id=project_id, document_id=document.document_id)
    assert "private" not in str(caught.value) and _ORIGINAL_KEY not in str(caught.value)
    with pytest.raises(DocumentSnapshotError, match="unavailable"):
        await read_frozen_document(source, project_id=project_id, document=frozen)
    assert frozen.content_hash == document.checksum == content.checksum


async def test_metadata_missing_never_reads_a_blob_or_looks_up_a_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """元 metadata の欠落だけが download の 404 であり、同名の別 ID を探さない。"""

    storage = InMemoryFileStorage()
    _metadata(monkeypatch, None, storage)
    get = AsyncMock(side_effect=AssertionError("No blob without metadata"))
    monkeypatch.setattr(storage, "get", get)
    service, source = _readers(storage)
    project_id, document_id = uuid4(), uuid4()
    with pytest.raises(DocumentNotFoundError):
        await service.download_document(project_id=project_id, document_id=document_id)
    assert await source.fetch(project_id=project_id, document_id=document_id) is None
    get.assert_not_called()


@pytest.mark.parametrize("field,value", [("size", -1), ("size", True), ("checksum", "invalid")])
async def test_invalid_stored_integrity_metadata_fails_before_blob_read(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    """保存 metadata が壊れていても無制限取得や新 hash の補署をしない。"""

    project_id = uuid4()
    document = stored_document(project_id, document_content())
    if field == "size":
        assert isinstance(value, int)
        changed = replace(document, size=value)
    else:
        assert field == "checksum" and isinstance(value, str)
        changed = replace(document, checksum=value)
    storage = InMemoryFileStorage()
    _metadata(monkeypatch, changed, storage)
    get = AsyncMock(side_effect=AssertionError("No blob with invalid metadata"))
    monkeypatch.setattr(storage, "get", get)
    service, _ = _readers(storage)
    with pytest.raises(DocumentContentInvalidError):
        await service.download_document(project_id=project_id, document_id=document.document_id)
    get.assert_not_called()


async def test_new_current_metadata_cannot_replace_original_frozen_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """現在の metadata と byte が一緒に変わっても、元の凍結 snapshot は失効する。"""

    project_id = uuid4()
    old = document_content(b"old")
    current = document_content(b"new", document_id=old.document_id)
    storage = InMemoryFileStorage()
    _metadata(monkeypatch, stored_document(project_id, current), storage)
    await storage.put(_ORIGINAL_KEY, current.data, content_type=current.mime)
    _, source = _readers(storage)
    frozen = document_snapshot(project_id, [old]).documents[0]
    with pytest.raises(DocumentSnapshotError, match="snapshot"):
        await read_frozen_document(source, project_id=project_id, document=frozen)
    assert frozen.content_hash == old.checksum


async def test_read_cancellation_is_not_reported_as_confirmed_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消を缺失や破損に変換せず、呼出元の監督へ返す。"""

    project_id = uuid4()
    content = document_content()
    storage = InMemoryFileStorage()
    _metadata(monkeypatch, stored_document(project_id, content), storage)
    monkeypatch.setattr(storage, "get", AsyncMock(side_effect=asyncio.CancelledError))
    service, source = _readers(storage)
    with pytest.raises(asyncio.CancelledError):
        await service.download_document(project_id=project_id, document_id=content.document_id)
    with pytest.raises(asyncio.CancelledError):
        await read_frozen_document(
            source,
            project_id=project_id,
            document=document_snapshot(project_id, [content]).documents[0],
        )


@pytest.mark.parametrize("failure", [None, "changed", "hash", "size", "namespace"])
async def test_source_preserves_observation_only_with_original_verified_bytes(
    monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    """取得 metadata と原 hash/namespace を同時に検証し、失敗時に通常 GET へ降格しない。"""

    project_id = uuid4()
    content = document_content(b"abc")
    storage = InMemoryFileStorage()
    _metadata(monkeypatch, stored_document(project_id, content), storage)
    observation = BlobObservation(
        last_modified=datetime(2026, 9, 11, tzinfo=UTC), etag="opaque",
        version_id="v1", size=2 if failure == "size" else 3, content_type=content.mime,
    )

    async def acquire(key: str, *, max_bytes: int) -> ObservedBlob:
        """原 key/上限を確認し、SDK 失敗・本文差替え・取得中の保存先変更を模倣する。"""

        assert key == _ORIGINAL_KEY and max_bytes == 3
        if failure == "changed":
            raise FileStorageError("Blob changed during acquisition")
        if failure == "namespace":
            monkeypatch.setattr(storage, "_namespace", InMemoryFileStorage().namespace)
        return ObservedBlob(b"xyz" if failure == "hash" else content.data, observation)

    observed = AsyncMock(side_effect=acquire)
    get = AsyncMock(side_effect=AssertionError("No downgrade to plain GET"))
    monkeypatch.setattr(storage, "get_observed", observed, raising=False)
    monkeypatch.setattr(storage, "get", get)
    _, source = _readers(storage)
    frozen = document_snapshot(project_id, [content]).documents[0]
    if failure is not None:
        with pytest.raises(DocumentSnapshotError, match="unavailable"):
            await read_frozen_document(source, project_id=project_id, document=frozen)
    else:
        acquired = await read_frozen_document(source, project_id=project_id, document=frozen)
        assert acquired == replace(
            content, observation=observation, source_object_key=_ORIGINAL_KEY
        )
    observed.assert_awaited_once()
    get.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [None, "project", "metadata", "hash", "observation", "key", "reported_key", "namespace"],
)
async def test_document_inspection_and_later_acquisition_keep_original_identity(
    monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    """本文なしの観測から原 Project/文書/metadata/hash を維持して後日取得する。"""

    project_id = uuid4()
    content = document_content(b"abc")
    document = stored_document(project_id, content)
    storage = InMemoryFileStorage()
    lookup = _metadata(monkeypatch, document, storage)
    observation = BlobObservation(
        last_modified=datetime(2026, 9, 11, tzinfo=UTC), etag="opaque",
        version_id="original-version", size=3, content_type=content.mime,
    )
    inspect = AsyncMock(return_value=observation)
    get = AsyncMock(side_effect=AssertionError("No ordinary GET"))
    acquired_observation = (
        replace(observation, etag="changed") if failure == "observation" else observation
    )
    acquired = AsyncMock(return_value=ObservedBlob(
        b"xyz" if failure == "hash" else content.data, acquired_observation
    ))
    monkeypatch.setattr(storage, "inspect", inspect, raising=False)
    monkeypatch.setattr(storage, "get_observed", acquired, raising=False)
    monkeypatch.setattr(storage, "get", get)
    _, source = _readers(storage)
    observed = await source.inspect(project_id=project_id, document_id=content.document_id)
    assert observed is not None and observed.document.content_hash == content.checksum
    assert observed.observation == observation
    assert observed.source_object_key == _ORIGINAL_KEY
    inspect.assert_awaited_once_with(_ORIGINAL_KEY)
    acquired.assert_not_called()
    if failure == "metadata":
        lookup.return_value = (
            replace(document, name="replaced.md"), BlobReference(_ORIGINAL_KEY, storage.namespace)
        )
    elif failure == "key":
        lookup.return_value = (document, BlobReference("replaced/key", storage.namespace))
    elif failure == "reported_key":
        observed = replace(observed, source_object_key="different/original")
    elif failure == "namespace":
        monkeypatch.setattr(storage, "_namespace", InMemoryFileStorage().namespace)
        lookup.return_value = (document, BlobReference(_ORIGINAL_KEY, storage.namespace))
    if failure is not None:
        with pytest.raises((DocumentSnapshotError, DocumentContentInvalidError)):
            await source.fetch_observed(
                project_id=uuid4() if failure == "project" else project_id, observed=observed
            )
    else:
        result = await source.fetch_observed(project_id=project_id, observed=observed)
        assert result == replace(content, observation=observation, source_object_key=_ORIGINAL_KEY)
    if failure in {"project", "metadata", "key", "reported_key", "namespace"}:
        acquired.assert_not_called()
    else:
        acquired.assert_awaited_once_with(_ORIGINAL_KEY, max_bytes=3, expected=observation)
    get.assert_not_called()


async def test_nonobserving_storage_cannot_fabricate_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metadata 非対応 backend では本文取得に逃げず、観測不能を明示する。"""

    project_id = uuid4()
    content = document_content(b"abc")
    storage = InMemoryFileStorage()
    _metadata(monkeypatch, stored_document(project_id, content), storage)
    get = AsyncMock(side_effect=AssertionError("No GET to fabricate observation"))
    monkeypatch.setattr(storage, "get", get)
    _, source = _readers(storage)
    with pytest.raises(DocumentStorageUnavailableError, match="observation is unavailable"):
        await source.inspect(project_id=project_id, document_id=content.document_id)
    get.assert_not_called()
