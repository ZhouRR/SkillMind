"""MinIO/S3 backed FileStorage の production 実装。

SDK seam は offline で検証し、実機 round-trip は承認済み bucket で別に確認する。
put 時に sha256 を object metadata へ書くが、文書読取は実 byte を独立に検証する。
"""

from __future__ import annotations

import asyncio
import io
from urllib.parse import urlparse

from minio import Minio
from minio.error import MinioException, S3Error
from urllib3.exceptions import HTTPError

from projectmind.core.hashing import sha256_hex
from projectmind.storage.blob import (
    BlobNotFoundError,
    BlobReadLimitExceededError,
    FileStorageError,
    StoredBlob,
    sanitize_object_key,
    validate_read_limit,
)

_SHA256_METADATA_KEY = "sha256"
_READ_CHUNK_BYTES = 64 * 1024


class S3FileStorage:
    """単一 bucket に blob を保存する MinIO/S3 backend。"""

    def __init__(self, *, endpoint: str, bucket: str, access_key: str, secret_key: str) -> None:
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

    async def get(self, key: str, *, max_bytes: int | None = None) -> bytes:
        """接続・有界読取・後片付けを一つの thread に閉じ、loop を塞がない。"""

        validate_read_limit(max_bytes)
        safe = sanitize_object_key(key)
        # await の取消は SDK thread の停止を証明しない。response は thread 自身が必ず閉じる。
        return await asyncio.to_thread(self._read, safe, max_bytes)

    def _read(self, key: str, max_bytes: int | None) -> bytes:
        """同一 GET の実 byte を上限 + 1 まで読み、SDK の内部詳細を外へ出さない。"""

        try:
            response = self._client.get_object(self._bucket, key)
            try:
                if max_bytes is None:
                    return bytes(response.read(decode_content=False))
                payload = bytearray()
                while len(payload) <= max_bytes:
                    chunk = response.read(
                        min(_READ_CHUNK_BYTES, max_bytes + 1 - len(payload)),
                        decode_content=False,
                    )
                    if not chunk:
                        return bytes(payload)
                    payload.extend(chunk)
                raise BlobReadLimitExceededError("Blob content exceeds the read limit")
            finally:
                try:
                    response.close()
                finally:
                    response.release_conn()
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                raise BlobNotFoundError("Blob content is not available") from error
            raise FileStorageError("Blob storage is unavailable") from error
        except (MinioException, HTTPError, OSError) as error:
            raise FileStorageError("Blob storage is unavailable") from error

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
