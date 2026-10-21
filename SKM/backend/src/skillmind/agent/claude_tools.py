"""共有 Gateway を Claude SDK の in-process MCP 工具へ適合する。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

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

    schema = _sdk_input_schema(registered.input_schema)
    return tool(registered.sdk_name.removeprefix("mcp__skillmind__"), description, schema)(handler)


def _sdk_input_schema(original: Mapping[str, Any]) -> dict[str, Any]:
    """CLI が拒否する最上位の組合せ制約を、常に成立する条件の内側へ移す。"""
    schema = deepcopy(dict(original))
    # SDK は type/properties の組で完全 Schema と簡略引数表を区別する。
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    if not any(key in schema for key in ("oneOf", "anyOf", "allOf")):
        return schema
    # 定義・識別子は root に残し、参照先と原契約の検証意味を保つ。
    public = {key: schema.pop(key) for key in ("$schema", "$id", "$defs") if key in schema}
    properties = dict(schema["properties"])
    for key in ("oneOf", "anyOf", "allOf"):
        for branch in schema.get(key, []):
            for name, definition in branch.get("properties", {}).items():
                previous = properties.get(name)
                properties[name] = definition if previous is None or previous == definition else {
                    "anyOf": [previous, definition],
                }
    return {**public, "type": "object", "properties": properties, "if": {}, "then": schema}
