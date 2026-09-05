"""Frozen Integration scope で Redmine issue.read/v1 を実行する observe Provider。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.agent.evidence import EvidenceDraft
from projectmind.agent.run_binding import (
    RunBindingError,
    load_bound_run_resource,
    resolve_binding_secret,
)
from projectmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolProviderError,
)
from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.effects.redmine import (
    EffectProviderTransportError,
    RedmineTransport,
)
from projectmind.integrations.domain import SCOPE_WILDCARD, scope_values_allow
from projectmind.integrations.secrets import DeploymentSecretResolver

_CORE_FIELDS = frozenset({"subject", "description", "status_id"})


class RedmineIssueReadProvider:
    """Agent から Secret/config を隠し、Run binding の issue/field scope 内だけを読む。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        transport: RedmineTransport,
        secret_resolver: DeploymentSecretResolver,
    ) -> None:
        """Database、HTTP transport と deployment Secret resolver を注入する。"""

        self._session_factory = session_factory
        self._transport = transport
        self._secret_resolver = secret_resolver

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """Bound issue を読み、logical URI と content hash の Evidence を返す。"""

        integration_id = context.tool.integration_id
        binding_id = context.tool.binding_id
        issue_ref = arguments.get("issue_ref")
        raw_fields = arguments.get("fields", [])
        if integration_id is None or binding_id is None or not isinstance(issue_ref, str):
            raise ToolProviderError(
                "invalid_request", "Redmine issue binding is invalid", retryable=False
            )
        if not isinstance(raw_fields, list) or not all(
            isinstance(item, str) for item in raw_fields
        ):
            raise ToolProviderError(
                "invalid_request", "Requested issue fields are invalid", retryable=False
            )
        async with self._session_factory() as session:
            # Binding 再検証は repository 読取と同一実装を経由する (片方だけ緩む余地を作らない)。
            try:
                bound = await load_bound_run_resource(
                    session,
                    project_id=context.project_id,
                    run_id=context.run_id,
                    binding_id=binding_id,
                    integration_id=integration_id,
                    provider="redmine",
                    capability="issue.read/v1",
                )
            except RunBindingError as error:
                raise ToolProviderError(
                    error.code, error.message, retryable=error.retryable
                ) from error
            integration = bound.integration
            scope = bound.scope
            issue_ids = list(scope.get("issue_ids", []))
            field_keys = list(scope.get("field_keys", []))
            # "*" は管理者が明示付与した全許可。field 全許可時は transport の射影 key を
            # 確定させるため、field 未指定の既定読取だけ core field に留める。
            fields_unrestricted = SCOPE_WILDCARD in field_keys
            allowed_fields = set(field_keys) | set(_CORE_FIELDS)
            requested_fields = (
                set(raw_fields)
                if raw_fields
                else (set(_CORE_FIELDS) if fields_unrestricted else allowed_fields)
            )
            if fields_unrestricted:
                # 予約 token は Redmine の field 名ではないため transport へ渡さない。
                requested_fields = {
                    field for field in requested_fields if field != SCOPE_WILDCARD
                } or set(_CORE_FIELDS)
            if not scope_values_allow(issue_ids, issue_ref) or (
                not fields_unrestricted
                and not requested_fields.issubset(allowed_fields)
            ):
                raise ToolProviderError(
                    "invalid_request",
                    "Issue read exceeds the frozen binding scope",
                    retryable=False,
                )
            try:
                api_key = await resolve_binding_secret(
                    session,
                    resolver=self._secret_resolver,
                    integration=integration,
                    required=True,
                )
            except RunBindingError as error:
                raise ToolProviderError(
                    error.code, error.message, retryable=error.retryable
                ) from error
        if api_key is None:
            raise ToolProviderError(
                "unavailable", "Redmine credential is not configured", retryable=False
            )
        base_url = integration.config.get("base_url")
        if not isinstance(base_url, str):
            raise ToolProviderError(
                "unavailable", "Redmine Integration config is invalid", retryable=False
            )
        try:
            snapshot = await self._transport.read_issue(
                base_url=base_url,
                api_key=api_key,
                issue_id=issue_ref,
                field_keys=tuple(sorted(requested_fields)),
            )
        except EffectProviderTransportError as error:
            raise ToolProviderError(
                "unavailable", "Redmine issue could not be read", retryable=error.retryable
            ) from error
        fields = dict(snapshot.fields)
        subject = fields.pop("subject", "")
        description = fields.pop("description", None)
        status_value = fields.pop("status_id", None)
        content = {
            "id": snapshot.issue_id,
            "subject": subject if isinstance(subject, str) else str(subject),
            "description": description,
            "status": str(status_value) if status_value is not None else None,
            "updated_at": snapshot.revision,
            "fields": fields,
        }
        logical_uri = f"redmine://integration/{integration_id}/issues/{issue_ref}"
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "redmine",
                "issue": {
                    **content,
                    "extensions": {
                        "integration_id": str(integration_id),
                        "integration_revision": integration.revision,
                    },
                },
                "warnings": [],
                "truncated": False,
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="issue",
                    source_uri=logical_uri,
                    source_locator={
                        "integration_id": str(integration_id),
                        "issue_id": issue_ref,
                        "revision": snapshot.revision,
                    },
                    content_hash=f"sha256:{sha256_hex(canonical_json(content))}",
                    excerpt=(
                        str(content["subject"])[:2_000] if content["subject"] else None
                    ),
                    metadata={"provider": "redmine", "integration_revision": integration.revision},
                ),
            ),
        )
