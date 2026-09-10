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

    if (
        type(document.size) is not int
        or document.size < 0
        or not isinstance(document.checksum, str)
        or _CHECKSUM.fullmatch(document.checksum) is None
    ):
        raise DocumentContentInvalidError("Document content metadata is invalid")
    require_document_storage(storage, reference)
    try:
        data = await storage.get(reference.key, max_bytes=document.size)
    except BlobNotFoundError as error:
        raise DocumentContentMissingError("Document content is missing") from error
    except BlobReadLimitExceededError as error:
        raise DocumentContentInvalidError("Document content does not match its metadata") from error
    except FileStorageError as error:
        raise DocumentStorageUnavailableError("Document storage is unavailable") from error
    require_document_storage(storage, reference)
    verify_document_bytes(data, size=document.size, checksum=document.checksum)
    return data


def require_document_storage(storage: FileStorage, reference: BlobReference) -> StorageNamespace:
    """新規保存・通常読取・凍結読取・削除で原 namespace の照合を共有する。"""

    try:
        return require_storage_namespace(storage, reference.namespace)
    except FileStorageError as error:
        raise DocumentStorageUnavailableError("Document storage is unavailable") from error
