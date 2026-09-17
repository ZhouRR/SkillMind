"""原 MCP 操作の只読照会を持ち、変更 method を公開しない。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from skillmind.agent.mcp_tools_source import StreamableHttpMcpToolsSource
from skillmind.effects.mcp_call import check_read_back, has_operation_identity, resolve_effect_id
from skillmind.integrations.mcp_tools import configured_tool, digest, parse_result, tool_access


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
    payload = json.loads(command.arguments_json)
    if not has_operation_identity(payload):
        raise ValueError("MCP original operation has no verifiable receipt identity")
    resolved = resolve_effect_id(payload, str(command.effect_id))
    check_read_back(resolved["read_back"], receipt.result)


async def lookup_operation(
    source: StreamableHttpMcpToolsSource,
    config: dict[str, Any],
    credential: str,
    command: McpOperationCommand,
) -> McpOperationReceipt | None:
    """原 requestId の状態だけを照会し、起動結果を現在プロセスから推測しない。"""
    payload = json.loads(command.arguments_json)
    if not has_operation_identity(payload):
        raise ValueError("MCP original operation has no verifiable receipt identity")
    reader = resolve_effect_id(payload, str(command.effect_id))["read_back"]
    configured_tool(config, {"tool_names": [reader["name"]]}, reader["name"])
    if tool_access(config, reader["name"]) != "read":
        raise ValueError("MCP receipt tool is not authorized for reading")
    result = parse_result(
        await source.call(config, credential, reader["name"], reader["arguments"])
    )
    receipt = McpOperationReceipt(result)
    try:
        validate_receipt(command, receipt)
    except ValueError:
        return None
    return receipt
