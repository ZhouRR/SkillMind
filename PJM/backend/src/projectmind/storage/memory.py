"""テストと offline 実行のための in-memory FileStorage 実装。"""

from __future__ import annotations

from projectmind.core.hashing import sha256_hex
from projectmind.storage.blob import (
    BlobNotFoundError,
    StoredBlob,
    sanitize_object_key,
)


class InMemoryFileStorage:
    """MinIO/S3 を使わず blob を辞書に保持する決定的 backend。"""

    def __init__(self) -> None:
        """key ごとの (正文, content_type) を保持する。"""

        self._blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """Key を安全化して正文を保存し、metadata を返す。"""

        safe = sanitize_object_key(key)
        payload = bytes(data)
        self._blobs[safe] = (payload, content_type)
        return _blob(safe, payload, content_type)

    async def get(self, key: str) -> bytes:
        """保存済み正文を返し、無ければ BlobNotFoundError を送出する。"""

        entry = self._blobs.get(sanitize_object_key(key))
        if entry is None:
            raise BlobNotFoundError(f"Blob not found: {key}")
        return entry[0]

    async def delete(self, key: str) -> None:
        """存在しなくてもエラーにせず削除する。"""

        self._blobs.pop(sanitize_object_key(key), None)

    async def exists(self, key: str) -> bool:
        """Key の存在有無を返す。"""

        return sanitize_object_key(key) in self._blobs

    async def stat(self, key: str) -> StoredBlob:
        """保存済み metadata を返し、無ければ BlobNotFoundError を送出する。"""

        entry = self._blobs.get(sanitize_object_key(key))
        if entry is None:
            raise BlobNotFoundError(f"Blob not found: {key}")
        return _blob(sanitize_object_key(key), entry[0], entry[1])


def _blob(key: str, data: bytes, content_type: str) -> StoredBlob:
    """正文から size と sha256 を計算した StoredBlob を作る。"""

    return StoredBlob(
        key=key,
        size=len(data),
        content_type=content_type,
        sha256=f"sha256:{sha256_hex(data)}",
    )
