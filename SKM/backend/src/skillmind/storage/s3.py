"""MinIO/S3 backed FileStorage の production 実装。

SDK seam は offline で検証し、実機 round-trip は承認済み bucket で別に確認する。
put 時に sha256 を object metadata へ書くが、文書読取は実 byte を独立に検証する。
"""

from __future__ import annotations

import asyncio
import io
from urllib.parse import urlparse
from uuid import UUID

from minio import Minio
from minio.error import MinioException, S3Error
from urllib3.exceptions import HTTPError
from urllib3.response import BaseHTTPResponse

from skillmind.core.hashing import sha256_hex
from skillmind.storage.blob import (
    BlobNotFoundError,
    BlobReadLimitExceededError,
    FileStorageError,
    StorageNamespace,
    StoredBlob,
    sanitize_object_key,
    validate_read_limit,
)
from skillmind.storage.namespace import canonical_s3_endpoint, make_s3_namespace
from skillmind.storage.observation import BlobObservation, ObservedBlob
from skillmind.storage.s3_observation import observation_from_headers

_SHA256_METADATA_KEY = "sha256"
_READ_CHUNK_BYTES = 64 * 1024


class S3FileStorage:
    """単一 bucket に blob を保存する MinIO/S3 backend。"""

    def __init__(
        self, *, endpoint: str, bucket: str, access_key: str, secret_key: str,
        namespace_id: UUID | None = None,
    ) -> None:
        """検証済み endpoint と非 credential の namespace を遅延接続 client に固定する。"""

        canonical = canonical_s3_endpoint(endpoint)
        parsed = urlparse(canonical)
        self._bucket = bucket
        self._namespace = make_s3_namespace(
            namespace_id=namespace_id, endpoint=canonical, bucket=bucket
        ) if namespace_id is not None else None
        self._client = Minio(
            parsed.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=parsed.scheme == "https",
        )

    @property
    def namespace(self) -> StorageNamespace | None:
        """旧未設定を新配置に推測で結び付けず、構築時の immutable な識別を返す。"""

        return self._namespace

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """成功した PUT だけに保存結果を返し、失敗から未保存や補償削除を推測しない。"""

        safe = sanitize_object_key(key)
        payload = bytes(data)
        digest = f"sha256:{sha256_hex(payload)}"
        try:
            await asyncio.to_thread(
                self._client.put_object,
                self._bucket,
                safe,
                io.BytesIO(payload),
                len(payload),
                content_type=content_type,
                metadata={_SHA256_METADATA_KEY: digest},
            )
        except (MinioException, HTTPError, OSError) as error:
            # 失敗応答や await の取消は遠端 PUT の停止・未保存を証明しない。
            raise FileStorageError("Blob storage is unavailable") from error
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
                return _read_response(response, max_bytes)
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

    async def inspect(self, key: str) -> BlobObservation:
        """本文を開かず実 HEAD だけを行い、取得前の選択基準に使う metadata を返す。"""

        return await asyncio.to_thread(self._inspect, sanitize_object_key(key))

    def _inspect(self, key: str) -> BlobObservation:
        """欠落だけを不存在に分類し、SDK 補完値から観測を作らない。"""

        try:
            return observation_from_headers(self._client.stat_object(self._bucket, key).metadata)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                raise BlobNotFoundError("Blob content is not available") from error
            raise FileStorageError("Blob storage is unavailable") from error
        except (MinioException, HTTPError, OSError) as error:
            raise FileStorageError("Blob storage is unavailable") from error

    async def get_observed(
        self, key: str, *, max_bytes: int, expected: BlobObservation | None = None
    ) -> ObservedBlob:
        """有界取得の前後を照合し、別 version の本文や時刻を混在させない。"""

        validate_read_limit(max_bytes)
        if max_bytes is None:
            raise FileStorageError("Blob read limit must be a non-negative integer")
        if expected is not None and not isinstance(expected, BlobObservation):
            raise FileStorageError("Blob observation is invalid")
        return await asyncio.to_thread(
            self._read_observed, sanitize_object_key(key), max_bytes, expected
        )

    def _read_observed(
        self, key: str, max_bytes: int, expected: BlobObservation | None
    ) -> ObservedBlob:
        """version 固定または HEAD/GET/HEAD の一致を要求し、置換を再試行しない。"""

        try:
            # 先の選択で固定された version は現行 HEAD に読み替えない。
            before = (
                expected if expected is not None and expected.version_pinned else self._inspect(key)
            )
            if expected is not None and before != expected:
                raise FileStorageError("Blob changed since inspection")
            if before.size > max_bytes:
                raise BlobReadLimitExceededError("Blob content exceeds the read limit")
            response = self._client.get_object(
                self._bucket, key,
                version_id=before.version_id if before.version_pinned else None,
            )
            try:
                acquired = observation_from_headers(response.headers)
                if acquired != before:
                    raise FileStorageError("Blob changed during acquisition")
                data = _read_response(response, max_bytes)
                if len(data) != acquired.size:
                    raise FileStorageError("Blob content does not match its observation")
            finally:
                try:
                    response.close()
                finally:
                    response.release_conn()
            if not before.version_pinned:
                after = self._inspect(key)
                if after != acquired:
                    raise FileStorageError("Blob changed during acquisition")
            return ObservedBlob(data=data, observation=acquired)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NoSuchVersion"}:
                raise BlobNotFoundError("Blob content is not available") from error
            raise FileStorageError("Blob storage is unavailable") from error
        except (MinioException, HTTPError, OSError) as error:
            raise FileStorageError("Blob storage is unavailable") from error

    async def delete(self, key: str) -> None:
        """元 object の明確な不存在だけを冪等成功とし、拒否や通信未知を隠さない。"""

        safe = sanitize_object_key(key)
        try:
            await asyncio.to_thread(self._client.remove_object, self._bucket, safe)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                return
            raise FileStorageError("Blob storage is unavailable") from error
        except (MinioException, HTTPError, OSError) as error:
            raise FileStorageError("Blob storage is unavailable") from error

    async def exists(self, key: str) -> bool:
        """元 object の明確な不存在だけを False とし、存否不明は例外に保つ。"""

        safe = sanitize_object_key(key)
        try:
            await asyncio.to_thread(self._client.stat_object, self._bucket, safe)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise FileStorageError("Blob storage is unavailable") from error
        except (MinioException, HTTPError, OSError) as error:
            raise FileStorageError("Blob storage is unavailable") from error
        return True

    async def stat(self, key: str) -> StoredBlob:
        """Metadata と正確な object 不存在を区別し、内部 key や SDK 詳細を公開しない。"""

        safe = sanitize_object_key(key)
        try:
            info = await asyncio.to_thread(self._client.stat_object, self._bucket, safe)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                raise BlobNotFoundError("Blob content is not available") from error
            raise FileStorageError("Blob storage is unavailable") from error
        except (MinioException, HTTPError, OSError) as error:
            raise FileStorageError("Blob storage is unavailable") from error
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


def _read_response(response: BaseHTTPResponse, max_bytes: int | None) -> bytes:
    """通常 GET と metadata 付き GET が同じ上限 + 1 の実 byte 検出を使う。"""

    if max_bytes is None:
        return bytes(response.read(decode_content=False))
    payload = bytearray()
    while len(payload) <= max_bytes:
        chunk = response.read(
            min(_READ_CHUNK_BYTES, max_bytes + 1 - len(payload)), decode_content=False,
        )
        if not chunk:
            return bytes(payload)
        payload.extend(chunk)
    raise BlobReadLimitExceededError("Blob content exceeds the read limit")
