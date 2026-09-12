"""通常 download と凍結 source が同じ実 byte 完全性検証を使う境界を定義する。"""

from __future__ import annotations

import re

from skillmind.core.hashing import sha256_hex
from skillmind.documents.domain import (
    DocumentContentInvalidError,
    DocumentContentMissingError,
    DocumentStorageUnavailableError,
    StoredDocument,
)
from skillmind.storage import (
    BlobNotFoundError,
    BlobReadLimitExceededError,
    BlobReference,
    FileStorage,
    FileStorageError,
    StorageNamespace,
)
from skillmind.storage.namespace import require_storage_namespace
from skillmind.storage.observation import (
    BlobObservation,
    InspectableFileStorage,
    ObservedFileStorage,
)

_CHECKSUM = re.compile(r"sha256:[0-9a-f]{64}")


def verify_document_bytes(data: bytes, *, size: int, checksum: str) -> None:
    """申告値を補正せず、返す byte 自体の size と hash の一致を要求する。"""

    if type(size) is not int or size < 0 or len(data) != size:
        raise DocumentContentInvalidError("Document content does not match its metadata")
    if not isinstance(checksum, str) or f"sha256:{sha256_hex(data)}" != checksum:
        raise DocumentContentInvalidError("Document content does not match its metadata")


async def read_document_bytes(
    storage: FileStorage, *, reference: BlobReference, document: StoredDocument
) -> bytes:
    """metadata の上限で一度だけ実 byte を読み、内部 locator を安定 error に閉じる。"""

    data, _ = await _read_document(storage, reference=reference, document=document, observe=False)
    return data


async def read_document_content(
    storage: FileStorage, *, reference: BlobReference, document: StoredDocument,
    expected: BlobObservation | None = None,
) -> tuple[bytes, BlobObservation | None]:
    """対応 backend の取得 metadata を原 bytes と検証し、非対応なら欠落を明示する。"""

    return await _read_document(
        storage, reference=reference, document=document, observe=True, expected=expected
    )


async def inspect_document_content(
    storage: FileStorage, *, reference: BlobReference, document: StoredDocument
) -> BlobObservation:
    """原 namespace の metadata だけを観測し、本文未検証の事実を分離して返す。"""

    _validate_document_metadata(document)
    require_document_storage(storage, reference)
    if not isinstance(storage, InspectableFileStorage):
        raise DocumentStorageUnavailableError("Document storage observation is unavailable")
    try:
        observation = await storage.inspect(reference.key)
    except BlobNotFoundError as error:
        raise DocumentContentMissingError("Document content is missing") from error
    except FileStorageError as error:
        raise DocumentStorageUnavailableError("Document storage is unavailable") from error
    require_document_storage(storage, reference)
    if observation.size != document.size:
        raise DocumentContentInvalidError("Document observation does not match its metadata")
    return observation


async def _read_document(
    storage: FileStorage, *, reference: BlobReference, document: StoredDocument, observe: bool,
    expected: BlobObservation | None = None,
) -> tuple[bytes, BlobObservation | None]:
    """通常 download と観測付き source の namespace・上限・hash 検証を共有する。"""

    _validate_document_metadata(document)
    require_document_storage(storage, reference)
    observation = None
    try:
        if expected is not None:
            if not observe or not isinstance(storage, InspectableFileStorage):
                raise DocumentStorageUnavailableError("Document storage observation is unavailable")
            acquired = await storage.get_observed(
                reference.key, max_bytes=document.size, expected=expected
            )
            data, observation = acquired.data, acquired.observation
            if observation != expected or observation.size != len(data):
                raise DocumentContentInvalidError("Document observation does not match its content")
        elif observe and isinstance(storage, ObservedFileStorage):
            acquired = await storage.get_observed(reference.key, max_bytes=document.size)
            data, observation = acquired.data, acquired.observation
            if observation.size != len(data):
                raise DocumentContentInvalidError("Document observation does not match its content")
        else:
            data = await storage.get(reference.key, max_bytes=document.size)
    except BlobNotFoundError as error:
        raise DocumentContentMissingError("Document content is missing") from error
    except BlobReadLimitExceededError as error:
        raise DocumentContentInvalidError("Document content does not match its metadata") from error
    except FileStorageError as error:
        raise DocumentStorageUnavailableError("Document storage is unavailable") from error
    require_document_storage(storage, reference)
    verify_document_bytes(data, size=document.size, checksum=document.checksum)
    return data, observation


def _validate_document_metadata(document: StoredDocument) -> None:
    """通常読取・観測・観測付き読取に同じ保存済み size/hash の前提を課す。"""

    if (
        type(document.size) is not int
        or document.size < 0
        or not isinstance(document.checksum, str)
        or _CHECKSUM.fullmatch(document.checksum) is None
    ):
        raise DocumentContentInvalidError("Document content metadata is invalid")


def require_document_storage(storage: FileStorage, reference: BlobReference) -> StorageNamespace:
    """新規保存・通常読取・凍結読取・削除で原 namespace の照合を共有する。"""

    try:
        return require_storage_namespace(storage, reference.namespace)
    except FileStorageError as error:
        raise DocumentStorageUnavailableError("Document storage is unavailable") from error
