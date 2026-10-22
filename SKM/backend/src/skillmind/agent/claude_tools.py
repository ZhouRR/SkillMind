"""共有 Gateway を Claude SDK の in-process MCP 工具へ適合する。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from skillmind.agent.claude_tool_schema import ClaudeToolInputSchema, fit_claude_description
from skillmind.agent.domain import RegisteredTool
from skillmind.core.hashing import canonical_json

if TYPE_CHECKING:
    from skillmind.agent.tool_gateway import ToolGateway


def create_gateway_server(
    tools: tuple[RegisteredTool, ...], gateway: ToolGateway, descriptions: Mapping[str, str],
) -> Any:
    """検証済み Run 工具を公開し、呼出しは原 Gateway の認可・監査へ戻す。"""
    return create_sdk_mcp_server("skillmind", version="0.1.0", tools=[
        _sdk_tool(registered, gateway, descriptions[registered.sdk_name]) for registered in tools
    ])


def _sdk_tool(
    registered: RegisteredTool, gateway: ToolGateway, description: str,
) -> SdkMcpTool[Any]:
    """SDK 固有の未型付け入力を、共有 Gateway に渡す JSON object へ限定する。"""
    async def handler(arguments: Any) -> dict[str, Any]:
        """原認可 callback と同じ SDK 名で処理し、非 object は実行前に返す。"""
        if not isinstance(arguments, Mapping):
            return {"content": [{"type": "text", "text": canonical_json({
                "status": "error", "code": "invalid_request",
                "message": "Tool arguments must be an object", "retryable": False,
            })}], "is_error": True}
        return await gateway.invoke_mcp(registered.sdk_name, arguments)

    return tool(
        registered.sdk_name.removeprefix("mcp__skillmind__"),
        fit_claude_description(description),
        ClaudeToolInputSchema(registered.input_schema).schema,
    )(handler)
