"""承認済み MCP 操作を一回送信し、明示された只読回読で確認する。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from uuid import UUID

from skillmind.agent.mcp_lease import McpDesktopBusyError, McpDesktopLeases
from skillmind.agent.mcp_tools_source import McpToolsError, StreamableHttpMcpToolsSource
from skillmind.core.hashing import canonical_json
from skillmind.core.logging import log_event
from skillmind.core.redaction import contains_sensitive_content, find_sensitive_key
from skillmind.effects.domain import (
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectProviderResult,
)
from skillmind.effects.mcp_call import (
    MCP_CALL,
    MCP_PROVIDER_VERSION,
    call_payload,
    check_read_back,
    has_operation_identity,
    resolve_effect_id,
)
from skillmind.effects.mcp_diagnostics import McpEffectFailure
from skillmind.effects.redmine import EffectProviderTransportError
from skillmind.integrations.mcp_errors import diagnostic_fields
from skillmind.integrations.mcp_readback import McpReadBackError
from skillmind.integrations.mcp_tools import McpResultError, configured_tool, parse_result


class McpCallProvider:
    """再 claim は照会のみ。応答不明な app 起動や操作を再送しない。"""

    def __init__(
        self,
        *,
        source: StreamableHttpMcpToolsSource,
        leases: McpDesktopLeases,
        authorize: Callable[[ClaimedEffectExecution, str], Awaitable[None]],
    ) -> None:
        """段階認可、通信境界、永続専有を必須にする。"""
        self._source, self._leases, self._authorize = source, leases, authorize

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """承認済み回読条件の確認を保存し、業務 PASS を推定しない。"""
        frozen = deepcopy(execution)
        if (
            frozen.capability_version != MCP_CALL
            or frozen.provider != "mcp"
            or frozen.attempt_no < 1
        ):
            raise ValueError("Invalid MCP effect")
        if not credential:
            raise EffectProviderTransportError("credential_unavailable", retryable=False)
        try:
            # 契約エラーの診断も現行権限を確認してから返す。
            await self._authorize(frozen, credential)
        except PermissionError:
            raise McpEffectFailure(retryable=False, diagnostic={
                "stage": "authorize", "reason": "authority_revoked",
                "action_attempted": frozen.attempt_no > 1, "call_response_received": False,
                "read_back_attempts": 0,
            }) from None
        try:
            payload = call_payload(
                operation=frozen.operation, target=frozen.target, changes=frozen.changes,
                precondition=frozen.precondition, verification=frozen.verification,
                scope=frozen.integration_scope, config=frozen.integration_config,
            )
        except ValueError as error:
            raise McpEffectFailure(retryable=False, diagnostic={
                "stage": "preflight",
                "reason": (error.reason if isinstance(error, McpReadBackError)
                           else "invalid_contract"),
                "action_attempted": frozen.attempt_no > 1, "call_response_received": False,
                "read_back_attempts": 0,
                **({"check_index": error.check_index}
                   if isinstance(error, McpReadBackError) else {}),
            }) from None
        payload_resolved = resolve_effect_id(payload, str(frozen.effect_execution_id))
        name, args = payload_resolved["name"], payload_resolved["arguments"]
        reader = payload_resolved["read_back"]
        endpoint = frozen.integration_config["server_url"]

        async def call(tool: str, params: dict[str, Any]) -> dict[str, Any]:
            """一回ごとに原承認を確認し、I/O 後の撤権時には結果を公開しない。"""
            await self._authorize(frozen, credential)
            configured_tool(frozen.integration_config, frozen.integration_scope, tool)
            result = parse_result(
                await self._source.call(frozen.integration_config, credential, tool, params),
                credential=credential,
            )
            await self._authorize(frozen, credential)
            return result

        result: dict[str, Any]
        stage, action_attempted = "authorize", frozen.attempt_no > 1
        sent: dict[str, Any] | None = None
        after: dict[str, Any] | None = None
        read_attempts = 0
        try:
            cancel_target = payload.get("cancel_target")
            if cancel_target is not None:
                if frozen.integration_id is None:
                    raise ValueError("MCP Integration is required")
                await self._leases.require_original_effect(
                    frozen.run_id,
                    frozen.integration_id,
                    cancel_target,
                )
            stage = "acquire"
            await self._leases.acquire(
                endpoint,
                frozen.run_id,
                begin_effect=frozen.effect_execution_id,
                cancel_target=UUID(cancel_target) if cancel_target is not None else None,
            )
            if frozen.attempt_no > 1 and not has_operation_identity(payload):
                raise ValueError("operation_identity_unavailable")
            # 再 claim では read_back だけを送り、変更を再送しない。
            if frozen.attempt_no == 1:
                stage, action_attempted = "call", True
                sent = await call(name, args)
            # 読取だけを最大 3 回。実操作はこの loop の外に置き、取消は握り潰さない。
            for read_attempts in range(1, 4):
                try:
                    stage = "read_back"
                    after = await call(reader["name"], reader["arguments"])
                    stage = "verify"
                    check_read_back(reader, after)
                    break
                except (McpToolsError, McpResultError, McpReadBackError) as error:
                    reason = error.reason
                    if read_attempts == 3 or reason not in {
                        "transport_unconfirmed", "remote_tool_error", "read_back_mismatch"
                    }:
                        raise
                    # 次回 call の前後にも同じ原批准・lease を再検証する。
                    await asyncio.sleep(0.5 * read_attempts)
            result = {"call": sent, "read_back": after}
            await self._authorize(frozen, credential)
            stage = "confirm"
            await self._leases.confirm(endpoint, frozen.run_id, frozen.effect_execution_id)
        except PermissionError:
            # 失権後に取得済み本文を公開しない。
            raise McpEffectFailure(retryable=False, diagnostic={
                "stage": stage, "reason": "authority_revoked",
                "action_attempted": action_attempted, "call_response_received": sent is not None,
                "read_back_attempts": read_attempts,
            }) from None
        except (McpToolsError, ValueError, McpDesktopBusyError) as error:
            # 失敗した外部読取でも撤権確認を省略せず、旧観測の公開を防ぐ。
            try:
                await self._authorize(frozen, credential)
            except PermissionError:
                raise McpEffectFailure(retryable=False, diagnostic={
                    "stage": stage, "reason": "authority_revoked",
                    "action_attempted": action_attempted,
                    "call_response_received": sent is not None, "read_back_attempts": read_attempts,
                }) from None
            reason = (
                error.reason if isinstance(error, (McpToolsError, McpResultError, McpReadBackError))
                else "desktop_busy" if isinstance(error, McpDesktopBusyError)
                else "operation_identity_unavailable"
                if frozen.attempt_no > 1 and stage == "acquire"
                else "invalid_contract"
            )
            diagnostic: dict[str, Any] = {
                "stage": stage, "reason": reason, "action_attempted": action_attempted,
                **diagnostic_fields(error),
                "call_response_received": (
                    sent is not None or (stage == "call" and (
                        isinstance(error, McpResultError) or reason == "invalid_response"
                    ))
                ),
                "read_back_attempts": read_attempts,
            }
            if isinstance(error, McpReadBackError):
                diagnostic["check_index"] = error.check_index
            log_event(logging.getLogger(__name__), logging.WARNING, "mcp.effect.failed",
                      run_id=frozen.run_id, effect_execution_id=frozen.effect_execution_id,
                      reason_code=reason, failure_stage=stage,
                      local_diagnostic_id=diagnostic["local_diagnostic_id"])
            observations = tuple(
                _observation(frozen, value, phase, credential)
                for phase, value in (("call_response", sent), ("read_back_response", after))
                if value is not None
            )
            raise McpEffectFailure(
                # Schema 不整合・欠損は再照会でも直らない。原 identity のある一時失敗だけ再 claim。
                retryable=has_operation_identity(payload) and reason in {
                    "transport_unconfirmed", "remote_tool_error", "read_back_mismatch"
                },
                diagnostic=diagnostic, observations=observations,
            ) from None
        return EffectProviderResult(
            before=_evidence(frozen, payload, "before"),
            after=_evidence(frozen, result, "after"),
            verification={
                "method": "READ_BACK",
                "matched_paths": ["/call"],
                "effect_id": str(frozen.effect_execution_id),
                "operation_status": "READ_BACK_CONFIRMED",
                "business_verdict": "NOT_EVALUATED",
                "replayed": frozen.attempt_no > 1,
            },
            replayed=frozen.attempt_no > 1,
        )


def _evidence(
    execution: ClaimedEffectExecution, content: dict[str, Any], phase: str
) -> EffectEvidenceDraft:
    """原 effect と回読事実を保存し、artifact の保存や PASS を合成しない。"""
    return EffectEvidenceDraft(
        evidence_type="resource",
        source_uri=f"mcp://integration/{execution.integration_id}/tools/{execution.target['locator']}",
        source_locator={
            "integration_id": str(execution.integration_id),
            "effect_id": str(execution.effect_execution_id),
            "tool_name": execution.target["locator"],
            "phase": phase,
        },
        content=content,
        excerpt=None,
        metadata={"provider": "mcp", "provider_version": MCP_PROVIDER_VERSION},
    )


def _observation(
    execution: ClaimedEffectExecution, content: dict[str, Any], phase: str, credential: str,
) -> EffectEvidenceDraft:
    """未確認の取得事実を保存し、機密らしい応答は本文全体を除外する。"""
    serialized = canonical_json(content)
    if (find_sensitive_key(content) is not None or contains_sensitive_content(serialized)
        or credential in serialized):
        content = {"omitted": True, "reason": "sensitive_content"}
    return _evidence(execution, {"verified": False, "response": content}, phase)
