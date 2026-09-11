"""原 Run binding の resource URI だけを読む mcp.read/v1 Provider。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.mcp_source import McpReadError, McpResourceSource, validate_mcp_contents
from skillmind.agent.run_binding import (
    BoundRunResource,
    RunBindingError,
    load_bound_run_resource,
    resolve_binding_secret,
)
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.integrations.secrets import DeploymentSecretResolver


class McpReadProvider:
    """遠端 tools の権限を持たず、有界の resource 内容だけを Evidence にする。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        source: McpResourceSource,
        secret_resolver: DeploymentSecretResolver,
    ) -> None:
        """Project の権限 DB、I/O source、非公開 Secret resolver を注入する。"""
        self._session_factory = session_factory
        self._source = source
        self._secret_resolver = secret_resolver

    async def _bound(self, context: RunToolContext) -> tuple[BoundRunResource, str | None]:
        """I/O の前後で原 binding と現在の optional credential を再検証する。"""
        if context.tool.integration_id is None or context.tool.binding_id is None:
            raise ToolProviderError("invalid_request", "MCP binding is invalid", retryable=False)
        try:
            async with self._session_factory() as session:
                bound = await load_bound_run_resource(
                    session,
                    project_id=context.project_id,
                    run_id=context.run_id,
                    binding_id=context.tool.binding_id,
                    integration_id=context.tool.integration_id,
                    provider="mcp",
                    capability="mcp.read/v1",
                )
                token = await resolve_binding_secret(
                    session,
                    resolver=self._secret_resolver,
                    integration=bound.integration,
                    required=False,
                )
        except RunBindingError as error:
            raise ToolProviderError(error.code, error.message, retryable=error.retryable) from None
        return bound, token

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """範囲を先に検証し、撤権/更新後に返った内容はモデルや Evidence に公開しない。"""
        uri = arguments.get("uri")
        if (
            not isinstance(uri, str)
            or not 1 <= len(uri) <= 2048
            or set(arguments) - {"uri", "purpose"}
        ):
            raise ToolProviderError(
                "invalid_request", "MCP read request is invalid", retryable=False
            )
        bound, token = await self._bound(context)
        if uri not in bound.scope.get("resource_uris", []):
            raise ToolProviderError(
                "scope_denied", "MCP resource is outside the frozen scope", retryable=False
            )
        try:
            contents = await self._source.read(bound.integration.config, token, uri)
            validate_mcp_contents(contents, uri)
        except McpReadError:
            raise ToolProviderError(
                "unavailable", "MCP resource could not be read", retryable=False
            ) from None
        current, current_token = await self._bound(context)
        if current != bound or current_token != token:
            raise ToolProviderError(
                "unavailable", "MCP binding changed during read", retryable=False
            )
        read_at = datetime.now(UTC).isoformat()
        content = {"uri": uri, "contents": list(contents), "read_at": read_at}
        checksum = f"sha256:{sha256_hex(canonical_json(content))}"
        return ProviderToolResult(
            response={"status": "success", "provider": "mcp", **content, "content_hash": checksum},
            evidence=(
                EvidenceDraft(
                    evidence_type="resource",
                    source_uri=uri,
                    source_locator={
                        "integration_id": str(bound.integration.integration_id),
                        "uri": uri,
                        "read_at": read_at,
                    },
                    content_hash=checksum,
                    metadata={"provider": "mcp", "binding_checksum": bound.checksum},
                ),
            ),
        )
