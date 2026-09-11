"""Run の固定 binding を使う PostgreSQL database.read/v1 Provider。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.postgres_source import (
    MAX_DATABASE_BYTES,
    DatabaseReadError,
    DatabaseSource,
    build_database_query,
)
from skillmind.agent.run_binding import (
    BoundRunResource,
    RunBindingError,
    load_bound_run_resource,
    resolve_binding_secret,
)
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.integrations.secrets import DeploymentSecretResolver


class DatabaseReadProvider:
    """モデルへ接続設定を渡さず、原テーブル範囲内の結果だけを証拠化する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        source: DatabaseSource,
        secret_resolver: DeploymentSecretResolver,
    ) -> None:
        """権限 DB、外部読取 source と Secret resolver を注入する。"""
        self._session_factory = session_factory
        self._source = source
        self._secret_resolver = secret_resolver

    async def _bound(self, context: RunToolContext) -> tuple[BoundRunResource, str]:
        """I/O の前後で同じ Run binding と現在の Secret 状態を検証する。"""
        if context.tool.integration_id is None or context.tool.binding_id is None:
            raise ToolProviderError(
                "invalid_request", "Database binding is invalid", retryable=False
            )
        try:
            async with self._session_factory() as session:
                bound = await load_bound_run_resource(
                    session,
                    project_id=context.project_id,
                    run_id=context.run_id,
                    binding_id=context.tool.binding_id,
                    integration_id=context.tool.integration_id,
                    provider="postgres",
                    capability="database.read/v1",
                )
                password = await resolve_binding_secret(
                    session,
                    resolver=self._secret_resolver,
                    integration=bound.integration,
                    required=True,
                )
        except RunBindingError as error:
            raise ToolProviderError(error.code, error.message, retryable=error.retryable) from None
        if password is None:
            raise ToolProviderError(
                "unavailable", "Database credential is unavailable", retryable=False
            )
        return bound, password

    async def execute(
        self,
        context: RunToolContext,
        arguments: Mapping[str, Any],
    ) -> ProviderToolResult:
        """明示テーブルを読み、撤権後や版変更後の結果を公開しない。"""
        try:
            query = build_database_query(arguments)
        except ValueError:
            raise ToolProviderError(
                "invalid_request", "Database read request is invalid", retryable=False
            ) from None
        bound, password = await self._bound(context)
        if query.table not in bound.scope.get("tables", []):
            raise ToolProviderError(
                "scope_denied", "Table is outside the frozen scope", retryable=False
            )
        try:
            rows = await self._source.read(bound.integration.config, password, query)
        except DatabaseReadError:
            raise ToolProviderError(
                "unavailable", "Database read could not be completed", retryable=False
            ) from None
        current, current_password = await self._bound(context)
        if current != bound or current_password != password:
            raise ToolProviderError(
                "unavailable", "Database binding changed during read", retryable=False
            )
        # SQL の出力制限とは別に、注入 source からの結果も公開前に有界性を検証する。
        if (
            len(rows.rows) > query.limit
            or len(canonical_json(rows.rows).encode("utf-8")) > MAX_DATABASE_BYTES
        ):
            raise ToolProviderError(
                "unavailable", "Database result exceeds the read limit", retryable=False
            )
        read_at = datetime.now(UTC).isoformat()
        content = {
            "table": query.table,
            "rows": list(rows.rows),
            "read_at": read_at,
            "truncated": rows.truncated,
        }
        checksum = f"sha256:{sha256_hex(canonical_json(content))}"
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "postgres",
                **content,
                "content_hash": checksum,
                "warnings": ["More rows may be available; this result is a bounded live read."]
                if rows.truncated
                else [],
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="database",
                    source_uri=(
                        f"postgres://integration/{bound.integration.integration_id}/tables/"
                        f"{quote(query.table, safe='')}"
                    ),
                    source_locator={
                        "integration_id": str(bound.integration.integration_id),
                        "table": query.table,
                        "read_at": read_at,
                        "offset": query.offset,
                        "row_count": len(rows.rows),
                    },
                    content_hash=checksum,
                    metadata={"provider": "postgres", "binding_checksum": bound.checksum},
                ),
            ),
        )
