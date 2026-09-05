"""MinIO/S3 backed FileStorage の production 実装。

MinIO を要するため offline 検証には含めない。実機 round-trip は Compose 環境で確認する。
put 時に sha256 を object metadata へ書き、stat で読み戻す。
"""

from __future__ import annotations

import asyncio
import io
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error

from projectmind.core.hashing import sha256_hex
from projectmind.storage.blob import BlobNotFoundError, StoredBlob, sanitize_object_key

_SHA256_METADATA_KEY = "sha256"


class S3FileStorage:
    """単一 bucket に blob を保存する MinIO/S3 backend。"""

    def __init__(
        self, *, endpoint: str, bucket: str, access_key: str, secret_key: str
    ) -> None:
        """Endpoint の scheme から secure 可否を決め、遅延接続の client を保持する。"""

        parsed = urlparse(endpoint)
        self._bucket = bucket
        self._client = Minio(
            parsed.netloc or parsed.path,
            access_key=access_key,
            secret_key=secret_key,
            secure=parsed.scheme == "https",
        )

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """Blob を保存し、sha256 を metadata へ書き込んで返す。"""

        safe = sanitize_object_key(key)
        payload = bytes(data)
        digest = f"sha256:{sha256_hex(payload)}"
        await asyncio.to_thread(
            self._client.put_object,
            self._bucket,
            safe,
            io.BytesIO(payload),
            len(payload),
            content_type=content_type,
            metadata={_SHA256_METADATA_KEY: digest},
        )
        return StoredBlob(key=safe, size=len(payload), content_type=content_type, sha256=digest)

    async def get(self, key: str) -> bytes:
        """Blob 正文を返し、無ければ BlobNotFoundError へ変換する。"""

        safe = sanitize_object_key(key)
        try:
            response = await asyncio.to_thread(self._client.get_object, self._bucket, safe)
        except S3Error as error:
            raise BlobNotFoundError(f"Blob not found: {key}") from error
        try:
            return bytes(response.read())
        finally:
            response.close()
            response.release_conn()

    async def delete(self, key: str) -> None:
        """存在しなくてもエラーにせず削除する。"""

        safe = sanitize_object_key(key)
        try:
            await asyncio.to_thread(self._client.remove_object, self._bucket, safe)
        except S3Error:
            return

    async def exists(self, key: str) -> bool:
        """Stat 成否で存在有無を返す。"""

        safe = sanitize_object_key(key)
        try:
            await asyncio.to_thread(self._client.stat_object, self._bucket, safe)
        except S3Error:
            return False
        return True

    async def stat(self, key: str) -> StoredBlob:
        """Metadata を返し、無ければ BlobNotFoundError を送出する。"""

        safe = sanitize_object_key(key)
        try:
            info = await asyncio.to_thread(self._client.stat_object, self._bucket, safe)
        except S3Error as error:
            raise BlobNotFoundError(f"Blob not found: {key}") from error
        metadata = getattr(info, "metadata", None) or {}
        digest = metadata.get(f"x-amz-meta-{_SHA256_METADATA_KEY}") or metadata.get(
            _SHA256_METADATA_KEY, ""
        )
        return StoredBlob(
            key=safe,
            size=int(getattr(info, "size", 0) or 0),
            content_type=str(getattr(info, "content_type", "") or "application/octet-stream"),
            sha256=str(digest),
        )
