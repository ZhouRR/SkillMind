"""原 Effect を識別する、無再送・単一 PUT の条件付き object 作成 client。

これは低層 port であり、配額予約・提案批准・文書公開・停止証明ではない。
v2 の専用 key は原 Effect/内容に固定し、旧 protocol への PUT は禁止する。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID
from xml.etree import ElementTree

import httpx
from minio.credentials.credentials import Credentials
from minio.helpers import DictType, check_bucket_name
from minio.signer import sign_v4_s3

from skillmind.core.hashing import sha256_hex
from skillmind.storage.blob import StorageNamespace
from skillmind.storage.effect_write import (
    ObjectWriteCommand,
    ObjectWriteConflictError,
    ObjectWriteReceipt,
    ObjectWriteUncertainError,
    validate_object_etag,
    validate_object_version,
)
from skillmind.storage.namespace import canonical_s3_endpoint, make_s3_namespace

_ERROR_BYTES = 8192
_TIMEOUT_SECONDS = 30
_Authorize = Callable[[], Awaitable[None]]


class S3ObjectWriteSource:
    """固定 client の宛先だけを使用し、モデル URL・redirect・SDK retry を許さない。"""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        namespace_id: UUID,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Composition の設定と test 用 transport を固定する。ここでは接続しない。"""

        self._endpoint = canonical_s3_endpoint(endpoint)
        try:
            check_bucket_name(bucket, strict=True)
        except ValueError as error:
            raise ValueError("Object storage bucket is invalid") from error
        if re.fullmatch(r"[a-z0-9-]{1,63}", region) is None:
            raise ValueError("Object storage signing region is invalid")
        self._bucket = bucket
        self._namespace = make_s3_namespace(
            namespace_id=namespace_id,
            endpoint=self._endpoint,
            bucket=bucket,
        )
        self._credentials = Credentials(access_key, secret_key)
        self._region = region
        self._transport = transport

    @property
    def namespace(self) -> StorageNamespace:
        """設定済みの原保存先世代を返す。DB の URL から client を作り直さない。"""

        return self._namespace

    async def create_once(
        self,
        command: ObjectWriteCommand,
        *,
        authorize: _Authorize,
    ) -> ObjectWriteReceipt:
        """条件 PUT を一回だけ送り、成功は原 object の実 byte 読取で確認する。"""

        self._validate(command)
        if command.protocol_version != 2:
            raise ValueError("Legacy document objects are read-only")
        await authorize()
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS), self._client() as client:
                # 途中の待機後に実行権を再確認し、全 metadata と条件 header も署名する。
                await authorize()
                headers = {
                    "If-None-Match": "*",
                    "Content-Type": command.content_type,
                    **{
                        f"x-amz-meta-{key}": value for key, value in command.origin_metadata.items()
                    },
                }
                response, _ = await self._request(
                    client,
                    "PUT",
                    command,
                    headers=headers,
                    content=command.content,
                    max_bytes=_ERROR_BYTES,
                )
                if response.status_code not in {200, 412}:
                    raise _uncertain()
                version_id = None
                etag = None
                if response.status_code == 200:
                    etag = _etag(response)
                    version_id = _version_id(response)
                # 412 も成功ではない。原 Effect の metadata/正文が一致する場合だけ採用する。
                await authorize()
                try:
                    receipt = await self._lookup(
                        client,
                        command,
                        version_id=version_id,
                        expected_etag=etag,
                    )
                except ObjectWriteConflictError as error:
                    if response.status_code == 200:
                        raise _uncertain() from error
                    raise
                if receipt is None:
                    raise _uncertain()
                await authorize()
                return receipt
        except (httpx.HTTPError, OSError, TimeoutError, PermissionError) as error:
            # PUT 開始後の失権/取消期限は未保存を証明しない。再送も補償 DELETE もしない。
            raise _uncertain() from error

    async def lookup(
        self,
        command: ObjectWriteCommand,
        *,
        authorize: _Authorize,
        version_id: str | None = None,
    ) -> ObjectWriteReceipt | None:
        """GET だけで原結果を核対する。不在は旧 PUT の停止・未到達を証明しない。"""

        self._validate(command)
        await authorize()
        if version_id is not None:
            _valid_version(version_id)
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS), self._client() as client:
                receipt = await self._lookup(client, command, version_id=version_id)
                await authorize()
                return receipt
        except (httpx.HTTPError, OSError, TimeoutError, PermissionError) as error:
            raise _uncertain() from error

    def _validate(self, command: ObjectWriteCommand) -> None:
        """原 request 内容と実 client の bucket/namespace を接続前に照合する。"""

        command.validate()
        if command.namespace != self._namespace or command.bucket != self._bucket:
            raise ValueError("Object effect storage namespace is unavailable")

    def _client(self) -> httpx.AsyncClient:
        """各操作の接続を閉じ、環境 proxy・redirect・自動再送を無効にする。"""

        return httpx.AsyncClient(
            transport=self._transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False),
            timeout=httpx.Timeout(10, connect=5),
            trust_env=False,
            follow_redirects=False,
        )

    async def _lookup(
        self,
        client: httpx.AsyncClient,
        command: ObjectWriteCommand,
        *,
        version_id: str | None = None,
        expected_etag: str | None = None,
    ) -> ObjectWriteReceipt | None:
        """S3 の hash metadata や ETag を本文の代用にせず、同一 GET を照合する。"""

        response, content = await self._request(
            client,
            "GET",
            command,
            headers={},
            version_id=version_id,
            max_bytes=len(command.content),
        )
        if response.status_code == 404:
            if _error_code(content) == ("NoSuchVersion" if version_id else "NoSuchKey"):
                return None
            raise _uncertain()
        if response.status_code != 200:
            raise _uncertain()
        actual_version = _version_id(response)
        etag = _etag(response)
        if version_id is not None and actual_version != version_id:
            raise _uncertain()
        if (
            (expected_etag is not None and etag != expected_etag)
            or response.headers.get("Content-Type") != command.content_type
            or any(
                response.headers.get(f"x-amz-meta-{key}") != value
                for key, value in command.origin_metadata.items()
            )
            or content != command.content
        ):
            raise ObjectWriteConflictError("Object does not match the original effect")
        return ObjectWriteReceipt(
            command.effect_id,
            command.request_checksum,
            command.object_key,
            command.content_checksum,
            len(content),
            command.content_type,
            etag,
            actual_version,
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        command: ObjectWriteCommand,
        *,
        headers: dict[str, str],
        max_bytes: int,
        content: bytes = b"",
        version_id: str | None = None,
    ) -> tuple[httpx.Response, bytes]:
        """送る URL そのものを既存 SigV4 実装で署名し、応答を有界 stream で読む。"""

        url = f"{self._endpoint}/{self._bucket}/{quote(command.object_key, safe='/')}"
        if version_id is not None:
            url += "?" + urlencode({"versionId": version_id}, quote_via=quote)
        parsed = urlsplit(str(httpx.URL(url)))
        date = datetime.now(UTC)
        digest = sha256_hex(content)
        signed: DictType = {
            **headers,
            "Host": parsed.netloc,
            "x-amz-date": date.strftime("%Y%m%dT%H%M%SZ"),
            "x-amz-content-sha256": digest,
        }
        sign_v4_s3(
            method=method,
            url=parsed,
            region=self._region,
            headers=signed,
            credentials=self._credentials,
            content_sha256=digest,
            date=date,
        )
        wire_headers: dict[str, str] = {}
        for key, value in signed.items():
            if not isinstance(value, str):
                raise _uncertain()
            wire_headers[key] = value
        async with client.stream(
            method, parsed.geturl(), headers=wire_headers, content=content
        ) as response:
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise _uncertain()
            limit = max_bytes if response.status_code == 200 else _ERROR_BYTES
            body = bytearray()
            async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                if len(body) + len(chunk) > limit:
                    raise _uncertain()
                body.extend(chunk)
            return response, bytes(body)


def _uncertain() -> ObjectWriteUncertainError:
    """接続・SDK・正文・bucket 名を公開エラーへ持ち出さない。"""

    return ObjectWriteUncertainError("Original object effect requires reconciliation")


def _etag(response: httpx.Response) -> str:
    """ETag は opaque な引用符付き validator として保存し、MD5 と仮定しない。"""

    try:
        return validate_object_etag(response.headers.get("ETag", ""))
    except ValueError as error:
        raise _uncertain() from error


def _valid_version(value: str) -> None:
    """version は URL 成分へ別途 encode し、未 version 化 marker を固定版と誤認しない。"""

    try:
        validate_object_version(value)
    except ValueError as error:
        raise _uncertain() from error


def _version_id(response: httpx.Response) -> str | None:
    """未 version 化応答も保持し、存在しない Version ID を作らない。"""

    value: str | None = response.headers.get("x-amz-version-id")
    if value is None or value == "null":
        return None
    _valid_version(value)
    return value


def _error_code(content: bytes) -> str | None:
    """有界の S3 Error だけを解釈し、汎用 proxy 404 を対象欠落と扱わない。"""

    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        return None
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    if root.tag.rsplit("}", 1)[-1] != "Error":
        return None
    codes = [node.text for node in root if node.tag.rsplit("}", 1)[-1] == "Code"]
    return codes[0] if len(codes) == 1 else None
