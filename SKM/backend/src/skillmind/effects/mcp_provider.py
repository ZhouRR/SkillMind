"""承認済み FlaUI 操作を一回送信し、原操作の状態で確認する。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from uuid import UUID

from skillmind.agent.mcp_lease import McpDesktopBusyError, McpDesktopLeases
from skillmind.agent.mcp_tools_source import McpToolsError, StreamableHttpMcpToolsSource
from skillmind.effects.domain import (
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectProviderResult,
)
from skillmind.effects.mcp_call import MCP_CALL, MCP_PROVIDER_VERSION, call_payload
from skillmind.effects.redmine import EffectProviderTransportError
from skillmind.integrations.mcp_tools import configured_tool, parse_result


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
        """外部結果とテスト判定を分離する。terminal ERROR も原操作の確定回执として保存する。"""
        frozen = deepcopy(execution)
        if (
            frozen.capability_version != MCP_CALL
            or frozen.provider != "mcp"
            or frozen.attempt_no < 1
        ):
            raise ValueError("Invalid MCP effect")
        if not credential:
            raise EffectProviderTransportError("credential_unavailable", retryable=False)
        payload = call_payload(
            operation=frozen.operation,
            target=frozen.target,
            changes=frozen.changes,
            precondition=frozen.precondition,
            verification=frozen.verification,
            scope=frozen.integration_scope,
            config=frozen.integration_config,
        )
        name, args = payload["name"], dict(payload["arguments"])
        if name == "execute_step":
            args["requestId"] = str(frozen.effect_execution_id)
        endpoint = frozen.integration_config["server_url"]

        async def call(tool: str, params: dict[str, Any]) -> dict[str, Any]:
            """一回ごとに原承認を確認し、I/O 後の撤権時には結果を公開しない。"""
            await self._authorize(frozen, credential)
            configured_tool(frozen.integration_config, frozen.integration_scope, tool)
            result = parse_result(
                await self._source.call(frozen.integration_config, credential, tool, params)
            )
            await self._authorize(frozen, credential)
            return result

        result: dict[str, Any]
        try:
            await self._authorize(frozen, credential)
            if name == "cancel_step":
                if frozen.integration_id is None:
                    raise ValueError("MCP Integration is required")
                await self._leases.require_original_step(
                    frozen.run_id, frozen.integration_id, args["requestId"]
                )
            await self._leases.acquire(
                endpoint,
                frozen.run_id,
                begin_effect=frozen.effect_execution_id,
                cancel_target=UUID(args["requestId"]) if name == "cancel_step" else None,
            )
            # requestId のない起動は後続照会から原操作との因果を証明できない。
            if frozen.attempt_no > 1 and name == "open_application":
                raise McpToolsError("Original application launch is unconfirmed")
            sent = await call(name, args) if frozen.attempt_no == 1 else None
            if name == "open_application":
                if (
                    sent is None
                    or sent.get("status") != "READY"
                    or type(sent.get("processId")) is not int
                    or sent["processId"] <= 0
                ):
                    raise McpToolsError("Application launch was not confirmed")
                after = await call("inspect_window", {"appId": args["appId"]})
                if after.get("status") not in {"READY", "WINDOW_SELECTION_REQUIRED"} or after.get(
                    "processId"
                ) != sent.get("processId"):
                    raise McpToolsError("Application read-back failed")
                result = {"call": sent, "read_back": after}
            else:
                after = await call("get_step_status", {"requestId": args["requestId"]})
                if after.get("requestId") != args["requestId"] or after.get("status") not in {
                    "COMPLETED",
                    "ERROR",
                    "TIMEOUT",
                    "CANCELLED",
                    "ABORTED",
                }:
                    raise McpToolsError("Original operation is not terminal")
                if name == "execute_step" and any(
                    after.get(key) != args[key] for key in ("appId", "operation")
                ):
                    raise McpToolsError("Original operation identity differs")
                if (
                    name == "cancel_step"
                    and not after.get("cancellationRequested")
                    and after["status"] != "CANCELLED"
                ):
                    raise McpToolsError("Cancellation cannot be confirmed")
                result = {"call": sent, "read_back": after}
            await self._authorize(frozen, credential)
            await self._leases.confirm(endpoint, frozen.run_id, frozen.effect_execution_id)
        except PermissionError:
            raise EffectProviderTransportError(
                "effect_authority_revoked", retryable=False
            ) from None
        except (McpToolsError, ValueError, McpDesktopBusyError):
            raise EffectProviderTransportError(
                "mcp_effect_unconfirmed", retryable=name != "open_application"
            ) from None
        return EffectProviderResult(
            before=_evidence(frozen, payload, "before"),
            after=_evidence(frozen, result, "after"),
            verification={
                "method": "READ_BACK",
                "matched_paths": ["/call"],
                "effect_id": str(frozen.effect_execution_id),
                "operation_status": after["status"],
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
        source_uri=f"mcp://integration/{execution.integration_id}/tools/{execution.operation}",
        source_locator={
            "integration_id": str(execution.integration_id),
            "effect_id": str(execution.effect_execution_id),
            "tool_name": execution.operation,
            "phase": phase,
        },
        content=content,
        excerpt=None,
        metadata={"provider": "mcp", "provider_version": MCP_PROVIDER_VERSION},
    )
