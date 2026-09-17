"""原 MCP 操作の只読照会を持ち、変更 method を公開しない。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from skillmind.agent.mcp_tools_source import StreamableHttpMcpToolsSource
from skillmind.integrations.mcp_tools import digest, parse_result


@dataclass(frozen=True, slots=True)
class McpOperationCommand:
    """元承認からのみ作る、再送権限のない operation identity。"""

    effect_id: UUID
    project_id: UUID
    run_id: UUID
    integration_id: UUID
    name: str
    arguments_json: str

    @property
    def request_checksum(self) -> str:
        """原 Run、宛先と引数を同じ checksum に含める。"""
        return digest(
            [
                str(self.effect_id),
                str(self.project_id),
                str(self.run_id),
                str(self.integration_id),
                self.name,
                json.loads(self.arguments_json),
            ]
        )


@dataclass(frozen=True, slots=True)
class McpOperationReceipt:
    """確定した操作状態。テストの PASS や platform APPLIED を宣言しない。"""

    result: dict[str, Any]


def validate_receipt(command: McpOperationCommand, receipt: McpOperationReceipt) -> None:
    """元 ID と操作内容が同じ terminal receipt 以外を拒否する。"""
    args = json.loads(command.arguments_json)
    request_id = str(command.effect_id) if command.name == "execute_step" else args.get("requestId")
    result = receipt.result
    if command.name not in {"execute_step", "cancel_step"} or result.get("requestId") != request_id:
        raise ValueError("MCP receipt identity differs")
    if result.get("status") not in {"COMPLETED", "ERROR", "TIMEOUT", "CANCELLED", "ABORTED"}:
        raise ValueError("MCP operation is not confirmed terminal")
    if command.name == "execute_step" and any(
        result.get(k) != args[k] for k in ("appId", "operation")
    ):
        raise ValueError("MCP receipt operation differs")
    if (
        command.name == "cancel_step"
        and not result.get("cancellationRequested")
        and result.get("status") != "CANCELLED"
    ):
        raise ValueError("MCP cancellation is not confirmed")


async def lookup_operation(
    source: StreamableHttpMcpToolsSource,
    config: dict[str, Any],
    credential: str,
    command: McpOperationCommand,
) -> McpOperationReceipt | None:
    """原 requestId の状態だけを照会し、起動結果を現在プロセスから推測しない。"""
    if command.name == "open_application":
        raise ValueError("Application launch has no queryable operation receipt")
    args = json.loads(command.arguments_json)
    request_id = str(command.effect_id) if command.name == "execute_step" else args["requestId"]
    result = parse_result(
        await source.call(config, credential, "get_step_status", {"requestId": request_id})
    )
    if result.get("requestId") == request_id and result.get("status") == "RUNNING":
        return None
    receipt = McpOperationReceipt(result)
    validate_receipt(command, receipt)
    return receipt
