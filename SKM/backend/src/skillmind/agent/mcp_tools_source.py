"""一つの固定 MCP endpoint で発見と明示された一 tool 呼出しを行う。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from skillmind.agent.mcp_source import BoundedMcpTransport, McpReadError
from skillmind.core.hashing import canonical_json
from skillmind.integrations.mcp_tools import digest, normalize_catalog, validate_tool_value


class McpToolsError(RuntimeError):
    """本文・URL・credential を含まない通信または契約エラー。"""

    def __init__(self, reason: str = "transport_unconfirmed") -> None:
        """固定の分類だけを保持し、低層の例外を出力しない。"""
        self.reason = (
            reason
            if reason in {"contract_changed", "invalid_arguments", "invalid_response"}
            else "transport_unconfirmed"
        )
        super().__init__(self.reason)


def _request_failure(error: Exception) -> McpToolsError:
    """SDK の task group が包んだ既知の分類だけを復元する。"""
    if isinstance(error, McpToolsError):
        return error
    if isinstance(error, ExceptionGroup):
        for child in error.exceptions:
            found = _request_failure(child)
            if found.reason != "transport_unconfirmed":
                return found
    return McpToolsError()


class _ToolTransport(BoundedMcpTransport):
    """resources/read を開かず、発見と一回の精確 call だけを通す。"""

    def __init__(
        self, endpoint: str, inner: httpx.AsyncBaseTransport, call: dict[str, Any] | None
    ) -> None:
        """送信許可を呼出し前に凍結する。SDK の再送も二回目は拒否する。"""
        super().__init__(endpoint, "", inner)
        self._call = call
        self._called = False
        self._cursors: set[str] = set()

    def _check_rpc(self, payload: dict[str, Any]) -> None:
        """Protocol の名前と引数を照合し、model の要求で method を切り替えない。"""
        method = payload.get("method")
        if method == "resources/read":
            raise McpReadError("Resources are not enabled by the tool transport")
        if method == "tools/list":
            params = payload.get("params", {})
            if set(params) - {"cursor"}:
                raise McpReadError("MCP discovery parameters are invalid")
            cursor = canonical_json(params)
            if cursor in self._cursors or len(self._cursors) >= 8:
                raise McpReadError("MCP discovery pagination exceeded its limit")
            self._cursors.add(cursor)
        elif method == "tools/call":
            if self._called or self._call is None or payload.get("params") != self._call:
                raise McpReadError("MCP call differs from the authorized request")
            self._called = True
        else:
            super()._check_rpc(payload)


class StreamableHttpMcpToolsSource:
    """SDK の protocol を有界 transport 内へ閉じ、外部 tools を SDK へ直接登録しない。"""

    def __init__(
        self, *, transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None
    ) -> None:
        """本番では redirect・proxy・retry を使わず、test のみ I/O を注入する。"""
        self._factory = transport_factory or (
            lambda: httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        )

    async def discover(self, config: Mapping[str, Any], token: str | None) -> dict[str, Any]:
        """初期化と tool 清單だけを取得し、業務操作は呼ばない。"""
        return await self._request(config, token, None, None)

    async def call(
        self, config: Mapping[str, Any], token: str | None, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """原清單の hash を再検証した後、一回だけ承認済み引数を送信する。"""
        return await self._request(
            config,
            token,
            {"name": name, "arguments": arguments},
            normalize_catalog(config.get("tool_catalog")),
        )

    async def _request(
        self,
        config: Mapping[str, Any],
        token: str | None,
        call: dict[str, Any] | None,
        expected: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """双方向 callback は登録せず、例外本文は transport 境界で封じる。"""
        try:
            headers = {"Accept-Encoding": "identity"}
            if token is not None:
                if (
                    not token
                    or not token.isascii()
                    or any(c.isspace() or ord(c) < 33 for c in token)
                ):
                    raise ValueError("Invalid credential")
                headers["Authorization"] = "Bearer " + token
            transport = _ToolTransport(config["server_url"], self._factory(), call)
            async with (
                asyncio.timeout(90 if call else 20),
                httpx.AsyncClient(
                    transport=transport,
                    headers=headers,
                    timeout=httpx.Timeout(5, read=75),
                    follow_redirects=False,
                    trust_env=False,
                ) as http,
                streamable_http_client(config["server_url"], http_client=http) as streams,
                ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=75)
                ) as client,
            ):
                initialized = await client.initialize()
                if initialized.capabilities.tools is None:
                    raise ValueError("Service has no tools")
                entries: list[dict[str, Any]] = []
                cursor = None
                for _ in range(8):
                    page = await client.list_tools(cursor=cursor)
                    entries.extend(
                        {
                            "name": tool.name,
                            "description": tool.description or "",
                            "input_schema": tool.inputSchema,
                            "output_schema": tool.outputSchema,
                        }
                        for tool in page.tools
                    )
                    if len(entries) > 100:
                        raise ValueError("Too many tools")
                    cursor = page.nextCursor
                    if cursor is None:
                        break
                else:
                    raise ValueError("Tool catalog is incomplete")
                observed = normalize_catalog(
                    {
                        "server": {
                            "name": initialized.serverInfo.name,
                            "version": initialized.serverInfo.version,
                        },
                        "tools": entries,
                    }
                )
                if call is None:
                    return observed
                if digest(observed) != digest(expected):
                    raise McpToolsError("contract_changed")
                tool = next(entry for entry in observed["tools"] if entry["name"] == call["name"])
                try:
                    validate_tool_value(tool["input_schema"], call["arguments"])
                except ValueError:
                    raise McpToolsError("invalid_arguments") from None
                result = await client.call_tool(call["name"], arguments=call["arguments"])
                content = [
                    item.model_dump(mode="json", exclude_none=True) for item in result.content
                ]
                # 画像・resource link を返しても自動取得はしない。
                if tool["output_schema"] is not None and not result.isError:
                    try:
                        validate_tool_value(tool["output_schema"], result.structuredContent)
                    except ValueError:
                        raise McpToolsError("invalid_response") from None
                return {
                    "is_error": bool(result.isError),
                    "content": content,
                    "structured_content": result.structuredContent,
                }
        except Exception as error:
            # CancelledError は変換しない。送信後の失敗を未実行と宣言しない。
            raise _request_failure(error) from None
