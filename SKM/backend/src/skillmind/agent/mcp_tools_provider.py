"""原 Run の工具清單と審査済み read-only 操作だけを公開する。"""

from __future__ import annotations

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
from skillmind.integrations.mcp_tools import (
    PROFILE,
    READ_TOOLS,
    configured_tool,
    digest,
    normalize_catalog,
    parse_result,
    validate_arguments,
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
        try:
            catalog = normalize_catalog(bound.integration.config.get("tool_catalog"))
            allowed = [
                configured_tool(bound.integration.config, bound.scope, name)
                for name in bound.scope.get("tool_names", [])
            ]
            if not allowed:
                raise ValueError("MCP tool permission is empty")
            if self._capability == "mcp.tools/v1":
                if set(arguments) - {"purpose"}:
                    raise ValueError("Invalid discovery request")
                actual = await self._source.discover(bound.integration.config, token)
                if digest(actual) != digest(catalog):
                    raise ValueError(
                        "MCP tool contract changed; rediscover and save the connection"
                    )
                data = {"profile": PROFILE, "tools": allowed, "catalog_hash": digest(catalog)}
                locator = {"catalog_hash": digest(catalog)}
            else:
                name = arguments.get("name")
                if (
                    not isinstance(name, str)
                    or name not in READ_TOOLS
                    or set(arguments) - {"name", "arguments", "purpose"}
                ):
                    raise ValueError("Only inspect_window and get_step_status are read-only tools")
                configured_tool(bound.integration.config, bound.scope, name)
                params = validate_arguments(name, arguments.get("arguments"))
                if name == "get_step_status":
                    await self._leases.require_original_step(
                        context.run_id, bound.integration.integration_id, params["requestId"]
                    )
                if name == "inspect_window":
                    await self._leases.acquire(
                        bound.integration.config["server_url"], context.run_id
                    )
                data = {
                    "name": name,
                    "result": parse_result(
                        await self._source.call(bound.integration.config, token, name, params)
                    ),
                }
                locator = {
                    "tool_name": name,
                    "arguments_hash": digest(params),
                    "arguments": params,
                    "result_status": data["result"].get("status"),
                    "window_title": data["result"].get("windowTitle"),
                }
        except ValueError:
            raise ToolProviderError(
                "invalid_request",
                "MCP tool scope, arguments or frozen contract is invalid",
                retryable=False,
            ) from None
        except (McpToolsError, McpDesktopBusyError):
            raise ToolProviderError(
                "unavailable",
                "MCP tool read could not be confirmed or the desktop is reserved",
                retryable=False,
            ) from None
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
