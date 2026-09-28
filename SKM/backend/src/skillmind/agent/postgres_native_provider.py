"""原生 SQL の読取結果を原権限・SQL identity・Run file に結び付ける。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from asyncpg import PostgresError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.database_errors import classify_database_error
from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.postgres_native import NativePostgresSource
from skillmind.agent.resource_files import request_from_file, require_file_access, store_response
from skillmind.agent.run_binding import (
    BoundRunResource,
    RunBindingError,
    load_bound_run_resource,
    resolve_binding_secret,
)
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.integrations.secrets import DeploymentSecretResolver


class NativeDatabaseProvider:
    """旧 table/column scope を原生 SQL の権限に転用しない。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        source: NativePostgresSource,
        secret_resolver: DeploymentSecretResolver,
    ) -> None:
        """共有 binding と専用 SQL client を注入する。"""
        self._sessions, self._source, self._resolver = sessions, source, secret_resolver

    async def _bound(self, context: RunToolContext) -> tuple[BoundRunResource, str]:
        """原生 SQL を管理者が選んだ接続にだけ capability を解決する。"""
        if context.tool.binding_id is None or context.tool.integration_id is None:
            raise ToolProviderError("invalid_request", "SQL binding is missing", retryable=False)
        try:
            async with self._sessions() as session:
                bound = await load_bound_run_resource(
                    session,
                    project_id=context.project_id,
                    run_id=context.run_id,
                    binding_id=context.tool.binding_id,
                    integration_id=context.tool.integration_id,
                    provider="postgres",
                    capability="database.query/v1",
                )
                if bound.integration.config.get("access_mode") != "native_sql":
                    raise ToolProviderError(
                        "scope_denied", "Connection does not permit native SQL", retryable=False
                    )
                credential = await resolve_binding_secret(
                    session, resolver=self._resolver, integration=bound.integration, required=True
                )
                if credential is None:
                    raise ToolProviderError(
                        "unavailable", "SQL credential is missing", retryable=False
                    )
                return bound, credential
        except RunBindingError as error:
            raise ToolProviderError(error.code, error.message, retryable=error.retryable) from None

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """完全な有界結果を file として渡し、truncated を隠さない。"""
        require_file_access(context)
        request = await request_from_file(context, arguments)
        bound, credential = await self._bound(context)

        async def authorize() -> None:
            """途中の撤権/接続変更でも正文を公開しない。"""
            if await self._bound(context) != (bound, credential):
                raise ToolProviderError("scope_denied", "SQL binding changed", retryable=False)

        try:
            result = await self._source.read(
                bound.integration.config, credential, request, bound.scope, authorize=authorize
            )
        except (
            SQLAlchemyError,
            PostgresError,
            OSError,
            TimeoutError,
            ValueError,
            PermissionError,
        ) as error:
            await authorize()
            diagnostic = classify_database_error(error, "row_read")
            code = (
                "scope_denied"
                if isinstance(error, PermissionError)
                else "invalid_request"
                if isinstance(error, ValueError)
                else diagnostic.code
            )
            message = (
                "Database account does not permit this SQL"
                if isinstance(error, PermissionError)
                else ("SQL or parameters are invalid; correct this request "
                      "without dropping required conditions")
                if isinstance(error, ValueError)
                else diagnostic.message
            )
            raise ToolProviderError(code, message, retryable=False) from None
        data = canonical_json(result).encode("utf-8")
        if credential.encode("utf-8") in data:
            raise ToolProviderError(
                "unavailable", "SQL result contains connection credentials", retryable=False
            )
        file = await store_response(context, data)
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "postgres",
                "file": file,
                "row_count": len(result["rows"]),
                "truncated": result["truncated"],
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="resource",
                    source_uri=f"postgres-query://{context.tool.integration_id}/{context.tool_call_id}",
                    source_locator={
                        "request_hash": "sha256:" + sha256_hex(canonical_json(request))
                    },
                    content_hash=file["content_hash"],
                    metadata={"file": file, "truncated": result["truncated"]},
                ),
            ),
        )
