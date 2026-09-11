"""固定 HTTP endpoint の MCP resource を有界に読む。SDK 型はこの I/O 境界に閉じる。"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

from skillmind.core.hashing import canonical_json

MAX_MCP_BYTES = 1_048_576
MAX_MCP_CONTENTS = 20
_MAX_HTTP_REQUESTS = 16
_MAX_TOTAL_WIRE_BYTES = 3 * MAX_MCP_BYTES
_READ_TIMEOUT_SECONDS = 20


class McpReadError(RuntimeError):
    """遠端の URL、Session、応答本文を公開しない読取エラー。"""


class McpResourceSource(Protocol):
    """MCP のネットワーク I/O と Run の権限/証拠を分離する。"""

    async def read(
        self, config: Mapping[str, Any], token: str | None, uri: str
    ) -> tuple[dict[str, str], ...]:
        """一つの固定 URI の text/blob を取得する。"""
        ...


@dataclass(slots=True)
class _WireBudget:
    """同時 SSE を含む一回の source 呼出し全体で受信量と HTTP 回数を共有する。"""

    received: int = 0
    requests: int = 0


class _LimitedStream(httpx.AsyncByteStream):
    """圧縮されていない byte stream を parser より前で制限する。"""

    def __init__(self, stream: httpx.AsyncByteStream, budget: _WireBudget) -> None:
        """原 stream の close と呼出し全体の budget を保持する。"""
        self._stream = stream
        self._budget = budget

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """一応答と複数応答の両方で、上限を超えた chunk を parser に渡さない。"""
        received = 0
        async for chunk in self._stream:
            received += len(chunk)
            self._budget.received += len(chunk)
            if received > MAX_MCP_BYTES or self._budget.received > _MAX_TOTAL_WIRE_BYTES:
                raise McpReadError("MCP response exceeds the read limit")
            yield chunk

    async def aclose(self) -> None:
        """取消や parser 失敗でも元の response を閉じる。"""
        await self._stream.aclose()


class BoundedMcpTransport(httpx.AsyncBaseTransport):
    """SDK の再接続/redirect も固定 endpoint と読取 method の上限内に留める。"""

    def __init__(self, endpoint: str, uri: str, inner: httpx.AsyncBaseTransport) -> None:
        """一回の呼出し専用 transport と明示された二つの宛先を保持する。"""
        self._endpoint = httpx.URL(endpoint)
        self._uri = uri
        self._inner = inner
        self._budget = _WireBudget()
        self._read_started = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """外部 I/O 前に要求を、I/O 後 parser 前に response header を検証する。"""
        self._budget.requests += 1
        if self._budget.requests > _MAX_HTTP_REQUESTS or request.url != self._endpoint:
            raise McpReadError("MCP request exceeds the connection boundary")
        if request.method == "POST":
            payload = json.loads(request.content)
            method = payload.get("method")
            if method == "resources/read":
                if self._read_started or payload.get("params", {}).get("uri") != self._uri:
                    raise McpReadError("MCP resource URI changed before read")
                self._read_started = True
            elif method not in {
                "initialize",
                "notifications/initialized",
                "notifications/cancelled",
            }:
                # SDK は非公開の roots/sampling/elicitation を拒否し、server ping には応答する。
                if (
                    method is not None
                    or "id" not in payload
                    or not ("result" in payload or "error" in payload)
                ):
                    raise McpReadError("MCP method is not permitted")
        elif request.method not in {"GET", "DELETE"}:
            raise McpReadError("MCP HTTP method is not permitted")
        response = await self._inner.handle_async_request(request)
        if (
            300 <= response.status_code < 400
            or response.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            await response.aclose()
            raise McpReadError("MCP response transport is not permitted")
        if not isinstance(response.stream, httpx.AsyncByteStream):
            raise McpReadError("MCP transport returned a non-async stream")
        response.stream = _LimitedStream(response.stream, self._budget)
        return response

    async def aclose(self) -> None:
        """所有する HTTP pool を閉じる。"""
        await self._inner.aclose()


def validate_mcp_contents(contents: tuple[dict[str, str], ...], uri: str) -> None:
    """注入 source も含めて、範囲外 URI・曖昧な text/blob・巨大応答を拒否する。"""
    if len(contents) > MAX_MCP_CONTENTS:
        raise McpReadError("MCP result exceeds the read limit")
    for item in contents:
        if (
            item.get("uri") != uri
            or set(item) - {"uri", "mime_type", "text", "blob"}
            or ("text" in item) == ("blob" in item)
            or any(not isinstance(value, str) for value in item.values())
            or len(item.get("mime_type", "")) > 200
        ):
            raise McpReadError("MCP returned invalid resource contents")
        if "blob" in item:
            try:
                base64.b64decode(item["blob"], validate=True)
            except ValueError:
                raise McpReadError("MCP returned an invalid resource blob") from None
    if len(canonical_json(contents).encode("utf-8")) > MAX_MCP_BYTES:
        raise McpReadError("MCP result exceeds the read limit")


class StreamableHttpMcpSource:
    """一回ごとに権限を持たない MCP session を作り、resource 一件だけを取得する。"""

    def __init__(
        self, *, transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None
    ) -> None:
        """テストだけが transport を差し替え、本番は proxy 環境を継承しない。"""
        self._transport_factory = transport_factory or (
            lambda: httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        )

    async def read(
        self, config: Mapping[str, Any], token: str | None, uri: str
    ) -> tuple[dict[str, str], ...]:
        """SDK の双方向 protocol を利用し、model/roots/elicitation callback は登録しない。"""
        try:
            parsed_uri = AnyUrl(uri)
            if str(parsed_uri) != uri:
                raise McpReadError("MCP resource URI is not canonical")
            headers = {"Accept-Encoding": "identity"}
            if token is not None:
                if (
                    not token
                    or not token.isascii()
                    or any(c.isspace() or ord(c) < 33 for c in token)
                ):
                    raise McpReadError("MCP credential is invalid")
                headers["Authorization"] = f"Bearer {token}"
            transport = BoundedMcpTransport(config["server_url"], uri, self._transport_factory())
            async with (
                asyncio.timeout(_READ_TIMEOUT_SECONDS),
                httpx.AsyncClient(
                    transport=transport,
                    headers=headers,
                    timeout=httpx.Timeout(5, read=10),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                streamable_http_client(config["server_url"], http_client=client) as streams,
                ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=10)
                ) as session,
            ):
                initialized = await session.initialize()
                if initialized.capabilities.resources is None:
                    raise McpReadError("MCP server does not support resource reads")
                response = await session.read_resource(parsed_uri)
                contents: list[dict[str, str]] = []
                for content in response.contents:
                    if set(content.model_extra or {}) & {"text", "blob"}:
                        raise McpReadError("MCP returned ambiguous resource contents")
                    item = {"uri": str(content.uri)}
                    if content.mimeType is not None:
                        item["mime_type"] = content.mimeType
                    if isinstance(content, types.TextResourceContents):
                        item["text"] = content.text
                    else:
                        item["blob"] = content.blob
                    contents.append(item)
                result = tuple(contents)
                validate_mcp_contents(result, uri)
                return result
        except Exception:
            # SDK の TaskGroup は transport/validation 失敗を ExceptionGroup に包む。
            # 唯一の外部境界で詳細を伏せ、BaseException の取消はそのまま伝播する。
            raise McpReadError("MCP resource read could not be completed") from None
