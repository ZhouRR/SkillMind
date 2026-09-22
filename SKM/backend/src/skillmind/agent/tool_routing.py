"""同じ能力の複数 frozen resource を、公開名を増やさず精確な key で選択する。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any

from skillmind.agent.domain import RegisteredTool

_RESOURCE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class ToolRouteError(ValueError):
    """接続情報を公開しない、修正可能な資源選択の誤り。"""


def provider_arguments(tool: RegisteredTool, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """外側の selector だけを取り除き、MCP や DB の業務引数を変更しない。"""
    result = deepcopy(dict(arguments))
    if tool.resource_key is not None:
        result.pop("resource_key", None)
    return result


def resource_identity(tool: RegisteredTool) -> dict[str, str]:
    """監査と復旧に使う原 binding の非機密な identity を返す。"""
    if tool.resource_key is None:
        return {}
    return {
        "resource_key": tool.resource_key,
        "binding_id": str(tool.binding_id),
        "integration_id": str(tool.integration_id),
        "provider": tool.provider,
    }


class ToolRouting:
    """実行は個別 binding、SDK 宣言は一能力一件。曖昧な省略を推測しない。"""

    def __init__(self, tools: Sequence[RegisteredTool]) -> None:
        """無名の重複は従来どおり拒否し、明示 key と frozen identity を必要とする。"""
        self._groups: dict[str, list[RegisteredTool]] = {}
        for tool in tools:
            if tool.resource_key is not None and (
                not isinstance(tool.resource_key, str)
                or _RESOURCE_KEY.fullmatch(tool.resource_key) is None
                or tool.integration_id is None
                or tool.binding_id is None
            ):
                raise ValueError("Resource tool requires a valid frozen key and binding")
            group = self._groups.setdefault(tool.sdk_name, [])
            if group:
                first = group[0]
                if (
                    first.resource_key is None
                    or tool.resource_key is None
                    or any(item.resource_key == tool.resource_key for item in group)
                ):
                    raise ValueError(f"Duplicate SDK tool name or resource key: {tool.sdk_name}")
                if tool.capability != first.capability or tool.input_schema != first.input_schema:
                    raise ValueError("Resource routes must share one capability contract")
            # 認可は元の frozen RegisteredTool を返す。SDK view の Schema だけ別途複写する。
            group.append(tool)

    def unambiguous(self, name: str) -> RegisteredTool | None:
        """拒否監査の帰属。複数候補なら接続を推測しない。"""
        group = self._groups.get(name)
        return group[0] if group and len(group) == 1 else None

    def resolve(self, name: str, arguments: Mapping[str, Any]) -> RegisteredTool:
        """一つなら省略可、複数なら原 slot key を必須にする。接続 ID は受け取らない。"""
        group = self._groups.get(name)
        if not group:
            raise ToolRouteError(f"Tool is not registered for this run: {name}")
        if group[0].resource_key is None:
            if group[0].integration_id is not None and "resource_key" in arguments:
                raise ToolRouteError("This tool has no named resource selector")
            # change.propose の resource_key は提案契約自身の値であり、ここで消費しない。
            return group[0]
        if "resource_key" not in arguments:
            if len(group) == 1:
                return group[0]
            raise ToolRouteError(
                "resource_key is required; choose one of: "
                + ", ".join(str(tool.resource_key) for tool in group)
            )
        key = arguments["resource_key"]
        for tool in group:
            if isinstance(key, str) and key == tool.resource_key:
                return tool
        raise ToolRouteError("resource_key is not registered for this tool in this Run")

    @property
    def sdk_tools(self) -> tuple[RegisteredTool, ...]:
        """選択可能 key を enum で示す。元の Provider 契約や resource binding は変えない。"""
        result: list[RegisteredTool] = []
        for group in self._groups.values():
            first = group[0]
            if first.resource_key is None:
                result.append(first)
                continue
            schema = deepcopy(dict(first.input_schema))
            schema.setdefault("properties", {})["resource_key"] = {
                "type": "string",
                "enum": [tool.resource_key for tool in group],
                "description": "Select this Run's frozen resource slot. Required when multiple resources provide this tool; never supply a connection URL or credentials.",
            }
            if len(group) > 1:
                schema["required"] = list(
                    dict.fromkeys([*schema.get("required", []), "resource_key"])
                )
            # SDK view は実行権でない。複数 binding の代表 identity を外へ流用させない。
            result.append(
                replace(
                    first,
                    input_schema=schema,
                    integration_id=None if len(group) > 1 else first.integration_id,
                    binding_id=None if len(group) > 1 else first.binding_id,
                    resource_key=None if len(group) > 1 else first.resource_key,
                )
            )
        return tuple(result)
