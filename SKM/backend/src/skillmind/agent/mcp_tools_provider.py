"""原 Run の工具清單と審査済み read-only 操作だけを公開する。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.mcp_lease import McpDesktopBusyError, McpDesktopLeases
from skillmind.agent.mcp_provider import McpReadProvider
from skillmind.agent.mcp_source import StreamableHttpMcpSource
from skillmind.agent.mcp_tools_source import McpToolsError, StreamableHttpMcpToolsSource
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.logging import log_event
from skillmind.integrations.mcp_errors import diagnostic_fields, remote_detail_message
from skillmind.integrations.mcp_tools import (
    PROFILE,
    McpResultError,
    configured_tool,
    configured_tools,
    digest,
    normalize_catalog,
    parse_result,
    tool_access,
    validate_arguments,
    validate_tool_value,
)
from skillmind.integrations.secrets import DeploymentSecretResolver


class McpToolsProvider:
    """発見と query を別 capability にし、変更 tool を gateway から呼ばせない。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        capability: str,
        source: StreamableHttpMcpToolsSource,
        secret_resolver: DeploymentSecretResolver,
        leases: McpDesktopLeases,
    ) -> None:
        """共有 binding 検証と desktop 専有を注入する。"""
        self._binding = McpReadProvider(
            sessions,
            source=StreamableHttpMcpSource(),
            secret_resolver=secret_resolver,
            capability=capability,
        )
        self._capability, self._source, self._leases = capability, source, leases

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """現在権限を前後で確認し、server の説明を権限として解釈しない。"""
        bound, token = await self._binding._bound(context)
        data: dict[str, Any]
        locator: dict[str, Any]
        stage = "configuration"
        try:
            if self._capability == "mcp.tools/v1":
                catalog = normalize_catalog(bound.integration.config.get("tool_catalog"))
                allowed = configured_tools(bound.integration.config, bound.scope)
                if not allowed:
                    raise ValueError("MCP tool permission is empty")
                stage = "arguments"
                if set(arguments) - {"purpose", "reserve_desktop"}:
                    raise ValueError("Invalid discovery request")
                reserve = arguments.get("reserve_desktop", False)
                if type(reserve) is not bool:
                    raise ValueError("Invalid reservation request")
                stage = "contract"
                actual = await self._source.discover(bound.integration.config, token)
                if digest(actual) != digest(catalog):
                    raise ValueError(
                        "MCP tool contract changed; rediscover and save the connection"
                    )
                data = {
                    "profile": PROFILE,
                    "tools": [
                        {**tool, "access": tool_access(bound.integration.config, tool["name"])}
                        for tool in allowed
                    ],
                    "catalog_hash": digest(catalog),
                }
                locator = {"catalog_hash": digest(catalog)}
                # 予約は SKM 内の同 endpoint だけを直列化する。Windows 全体の専有ではない。
                desktop = None
                if reserve:
                    stage = "configuration"
                    desktop = await self._leases.acquire(
                        bound.integration.config["server_url"], context.run_id
                    )
                data.update(
                    {
                        "server": dict(actual["server"]),
                        "connection_ref": str(bound.integration.integration_id),
                        "binding_ref": str(context.tool.binding_id),
                        "desktop": desktop,
                        "limits": {
                            "tool_call_timeout_seconds": 90,
                        },
                    }
                )
                locator.update({"server": dict(actual["server"]), "desktop": desktop})
            else:
                stage = "arguments"
                name = arguments.get("name")
                if (
                    not isinstance(name, str)
                    or tool_access(bound.integration.config, name) != "read"
                    or set(arguments) - {"name", "arguments", "purpose"}
                ):
                    raise ValueError("MCP tool is not authorized for read-only calls")
                stage = "configuration"
                tool = configured_tool(bound.integration.config, bound.scope, name)
                stage = "arguments"
                params = validate_arguments(name, arguments.get("arguments"))
                validate_tool_value(tool["input_schema"], params)
                stage = "result"
                data = {
                    "name": name,
                    "result": parse_result(
                        await self._source.call(bound.integration.config, token, name, params),
                        credential=token,
                    ),
                }
                locator = {
                    "tool_name": name,
                    "arguments_hash": digest(params),
                    "arguments": params,
                    "result_status": data["result"].get("status"),
                    "window_title": data["result"].get("windowTitle"),
                }
        except (ValueError, McpToolsError, McpDesktopBusyError) as error:
            # エラーにも同じ撤権境界を適用し、認可後だけ脱敏済み診断を返す。
            current, current_token = await self._binding._bound(context)
            if current != bound or current_token != token:
                raise ToolProviderError(
                    "scope_denied", "MCP binding changed during read", retryable=False
                ) from None
            failure = _read_error(error, stage)
            log_event(
                logging.getLogger(__name__),
                logging.WARNING,
                "mcp.query.failed",
                run_id=context.run_id,
                tool_call_id=context.tool_call_id,
                reason_code=(failure.diagnostic or {}).get("reason"),
                local_diagnostic_id=(failure.diagnostic or {}).get("local_diagnostic_id"),
            )
            raise failure from None
        current, current_token = await self._binding._bound(context)
        if current != bound or current_token != token:
            raise ToolProviderError(
                "scope_denied", "MCP binding changed during read", retryable=False
            )
        observed_at = datetime.now(UTC).isoformat()
        response = {"status": "success", "provider": "mcp", **data, "observed_at": observed_at}
        return ProviderToolResult(
            response=response,
            evidence=(
                EvidenceDraft(
                    evidence_type="resource",
                    source_uri=f"mcp://integration/{bound.integration.integration_id}/tools",
                    source_locator={
                        "integration_id": str(bound.integration.integration_id),
                        **locator,
                        "observed_at": observed_at,
                    },
                    content_hash=digest(response),
                    metadata={"provider": "mcp", "binding_checksum": bound.checksum},
                ),
            ),
        )


def _read_error(error: Exception, stage: str) -> ToolProviderError:
    """再送許可と原因を混同せず、固定診断と有界の遠端データを返す。"""
    reason = error.reason if isinstance(error, (McpResultError, McpToolsError)) else stage
    if isinstance(error, McpDesktopBusyError):
        reason = "desktop_busy"
    messages = {
        "configuration": ("invalid_request", "MCP configured profile or tool scope is invalid."),
        "arguments": (
            "invalid_request",
            "MCP arguments are invalid. Check the frozen tool schema.",
        ),
        "invalid_arguments": (
            "invalid_request",
            "MCP arguments do not match the frozen tool schema.",
        ),
        "contract": (
            "invalid_request",
            "MCP catalog changed. Save the new connection contract and start a new Run.",
        ),
        "contract_changed": (
            "invalid_request",
            "MCP catalog changed. Save the new connection contract and start a new Run.",
        ),
        "desktop_busy": (
            "unavailable",
            "MCP desktop is reserved or the original operation cannot be accessed. "
            "Do not replay an action.",
        ),
        "protocol_error": ("unavailable", "MCP returned a JSON-RPC error. Do not replay actions."),
        "remote_tool_error": (
            "unavailable",
            "The remote MCP tool returned an execution error; this is not a local scope "
            "or argument rejection. Check the service diagnostics; do not assume "
            "the operation was not executed or replay actions.",
        ),
        "result": (
            "unavailable",
            "MCP response does not match the result contract. Do not replay actions.",
        ),
        "invalid_response": (
            "unavailable",
            "MCP response does not match the result contract. Do not replay actions.",
        ),
        "transport_unconfirmed": (
            "unavailable",
            "MCP communication or response could not be confirmed. Do not replay actions.",
        ),
    }
    code, message = messages[reason]
    diagnostic = {"kind": "mcp", "reason": reason, **diagnostic_fields(error)}
    message += f" SKM diagnostic ID: {diagnostic['local_diagnostic_id']}."
    message += remote_detail_message(diagnostic["remote_detail"])
    return ToolProviderError(code, message, retryable=False, diagnostic=diagnostic)
