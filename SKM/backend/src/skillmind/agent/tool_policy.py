"""登録済み MCP Tool の自動実行を決定する hard boundary を実装する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator

from skillmind.agent.domain import RegisteredTool

DENIED_BUILTIN_TOOLS = frozenset(
    {"Read", "Glob", "Grep", "Bash", "Write", "Edit", "WebFetch", "WebSearch"}
)
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*/v[1-9][0-9]*$")
_BOUNDARY_ARGUMENTS = frozenset(
    {"project_id", "integration_id", "provider", "secret_ref", "credential", "auth_token", "cwd"}
)


class ToolPolicyViolation(PermissionError):
    """Tool 呼び出しが Run snapshot の hard boundary に違反したことを表す。"""


def capability_to_sdk_name(capability: str) -> str:
    """Version 付き capability を衝突しない MCP Tool 名へ変換する。"""

    if _CAPABILITY_PATTERN.fullmatch(capability) is None:
        raise ValueError(f"Invalid versioned capability: {capability}")
    local_name = re.sub(r"[^a-z0-9]+", "_", capability).strip("_")
    return f"mcp__skillmind__{local_name}"


class ToolExecutionPolicy:
    """一つの Run に登録された read-only Tool だけを許可する。"""

    def __init__(
        self,
        tools: tuple[RegisteredTool, ...],
        *,
        allowed_capabilities: frozenset[str] | None = None,
    ) -> None:
        """Tool 名、capability、Schema の整合性を起動前に検証する。"""

        self._tools: dict[str, RegisteredTool] = {}
        for tool in tools:
            if allowed_capabilities is not None and tool.capability not in allowed_capabilities:
                raise ValueError(
                    f"Tool capability is absent from permission snapshot: {tool.capability}"
                )
            expected_name = capability_to_sdk_name(tool.capability)
            if tool.sdk_name != expected_name:
                raise ValueError(
                    f"SDK tool name does not match capability: {tool.sdk_name} != {expected_name}"
                )
            if not tool.read_only:
                raise ValueError(f"M0 only accepts read-only tools: {tool.capability}")
            if tool.sdk_name in self._tools:
                raise ValueError(f"Duplicate SDK tool name: {tool.sdk_name}")
            Draft202012Validator.check_schema(tool.input_schema)
            self._tools[tool.sdk_name] = tool

    @property
    def allowed_sdk_names(self) -> tuple[str, ...]:
        """ClaudeAgentOptions に渡す決定的な自動許可リストを返す。"""

        return tuple(sorted(self._tools))

    def registered(self, tool_name: str) -> RegisteredTool | None:
        """監査用に SDK 名へ対応する Run-scoped Tool を返す。"""

        return self._tools.get(tool_name)

    def authorize(self, tool_name: str, tool_input: Mapping[str, Any]) -> RegisteredTool:
        """Tool 名、parameter Schema、境界変更用 field を実行直前に検証する。"""

        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolPolicyViolation(f"Tool is not registered for this run: {tool_name}")
        boundary_fields = _find_boundary_fields(tool_input)
        if boundary_fields:
            fields = ", ".join(sorted(boundary_fields))
            raise ToolPolicyViolation(f"Tool input attempts to change run boundary: {fields}")
        errors = sorted(
            Draft202012Validator(tool.input_schema).iter_errors(dict(tool_input)),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            raise ToolPolicyViolation("Tool input does not match registered schema")
        return tool


def _find_boundary_fields(value: Any) -> set[str]:
    """入れ子の入力から model が変更してはならない platform field を抽出する。"""

    if isinstance(value, Mapping):
        found = {str(key) for key in value if str(key).lower() in _BOUNDARY_ARGUMENTS}
        for nested in value.values():
            found.update(_find_boundary_fields(nested))
        return found
    if isinstance(value, list | tuple):
        nested_found: set[str] = set()
        for nested in value:
            nested_found.update(_find_boundary_fields(nested))
        return nested_found
    return set()
