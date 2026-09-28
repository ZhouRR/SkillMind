"""登録 HTTP 資源の読取を原 binding と実応答 file に結び付ける。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.http_source import HttpResourceError, HttpResourceSource
from skillmind.agent.resource_files import request_from_file, require_file_access, store_response
from skillmind.agent.run_binding import (
    BoundRunResource,
    RunBindingError,
    load_bound_run_resource,
    resolve_binding_secret,
)
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.integrations.secrets import DeploymentSecretResolver


class HttpReadProvider:
    """body は model message に載せず、呼出しごとの完全な file として渡す。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        source: HttpResourceSource,
        secret_resolver: DeploymentSecretResolver,
    ) -> None:
        """共通 binding 検証と Worker 内 client を使う。"""
        self._sessions, self._source, self._resolver = sessions, source, secret_resolver

    async def _bound(self, context: RunToolContext) -> tuple[BoundRunResource, str | None]:
        """I/O 前後で現在権限と凍結接続を確認する。"""
        if context.tool.binding_id is None or context.tool.integration_id is None:
            raise ToolProviderError("invalid_request", "HTTP binding is missing", retryable=False)
        try:
            async with self._sessions() as session:
                bound = await load_bound_run_resource(
                    session,
                    project_id=context.project_id,
                    run_id=context.run_id,
                    binding_id=context.tool.binding_id,
                    integration_id=context.tool.integration_id,
                    provider="http",
                    capability="http.read/v1",
                )
                token = await resolve_binding_secret(
                    session,
                    resolver=self._resolver,
                    integration=bound.integration,
                    required=bound.integration.config["auth_mode"] != "none",
                )
                return bound, token
        except RunBindingError as error:
            raise ToolProviderError(error.code, error.message, retryable=error.retryable) from None

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """HTTP error status も実応答として返し、業務成功と混同しない。"""
        require_file_access(context)
        request = await request_from_file(context, arguments)
        request.pop("purpose", None)
        bound, credential = await self._bound(context)
        try:
            result = await self._source.request(
                bound.integration.config, bound.scope, request, credential
            )
        except (ValueError, HttpResourceError) as error:
            await self._same_bound(context, bound, credential)
            raise ToolProviderError(
                "invalid_request" if isinstance(error, ValueError) else "unavailable",
                "HTTP request could not be completed",
                retryable=False,
            ) from None
        await self._same_bound(context, bound, credential)
        file = await store_response(context, result.body, suffix="txt")
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "http",
                "http_status": result.status,
                "headers": result.headers,
                "file": file,
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="resource",
                    source_uri=f"http-resource://{context.tool.integration_id}/{context.tool_call_id}",
                    source_locator={
                        "path": request["path"],
                        "method": request.get("method", "GET"),
                    },
                    content_hash=file["content_hash"],
                    metadata={"http_status": result.status, "file": file},
                ),
            ),
        )

    async def _same_bound(
        self, context: RunToolContext, bound: BoundRunResource, credential: str | None
    ) -> None:
        """撤権後の応答正文を file にも公開しない。"""
        if await self._bound(context) != (bound, credential):
            raise ToolProviderError(
                "scope_denied", "HTTP binding changed during access", retryable=False
            )
