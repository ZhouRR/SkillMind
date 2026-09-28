"""Worker から登録済み HTTP API へ一度だけ送信する有界 client。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from skillmind.core.hashing import canonical_json
from skillmind.integrations.http_resource import validate_request

MAX_HTTP_BYTES = 16 * 1024 * 1024


class HttpResourceError(RuntimeError):
    """URL、header、応答正文を例外に反射しない固定診断。"""


@dataclass(frozen=True)
class HttpResponse:
    """接続 client が実際に受信した応答。成功した業務処理とは区別する。"""

    status: int
    headers: dict[str, str]
    body: bytes


class HttpResourceSource:
    """redirect・自動再送・環境 proxy を使わず、credential は登録先だけへ送る。"""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """隔離回帰だけが transport を差し替える。"""
        self._transport = transport

    async def request(
        self,
        config: Mapping[str, Any],
        scope: Mapping[str, Any],
        request: Mapping[str, Any],
        credential: str | None,
        *,
        write: bool = False,
    ) -> HttpResponse:
        """read/write 共用の実 I/O。timeout 後も write の再送を行わない。"""
        value = validate_request(config, scope, request, write=write)
        headers = {**value["headers"], "Accept-Encoding": "identity"}
        mode = config["auth_mode"]
        if mode != "none":
            if not credential or any(ord(c) < 32 or ord(c) == 127 for c in credential):
                raise HttpResourceError("credential_unavailable")
            if mode == "bearer":
                headers["Authorization"] = "Bearer " + credential
            else:
                headers[config["credential_header"]] = credential
        body = canonical_json(value["body"]).encode("utf-8") if "body" in value else None
        if body is not None:
            if len(body) > MAX_HTTP_BYTES:
                raise HttpResourceError("request_too_large")
            if not any(key.lower() == "content-type" for key in headers):
                headers["Content-Type"] = "application/json"
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport, trust_env=False, follow_redirects=False, timeout=30.0
                ) as client,
                client.stream(
                    value["method"],
                    value["url"],
                    params=value["query"],
                    headers=headers,
                    content=body,
                ) as response,
            ):
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_HTTP_BYTES:
                        raise HttpResourceError("response_too_large")
                data = bytes(chunks)
                if credential and credential.encode("utf-8") in data:
                    raise HttpResourceError("credential_in_response")
                safe_headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key in {"content-type", "etag", "last-modified", "retry-after"}
                    and (not credential or credential not in value)
                }
                return HttpResponse(response.status_code, safe_headers, data)
        except httpx.HTTPError:
            raise HttpResourceError("transport_unconfirmed") from None
