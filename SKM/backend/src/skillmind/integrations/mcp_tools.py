"""MCP の保存済み Schema と管理者の工具別権限を検証する。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
from typing import Any

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.integrations.mcp_errors import local_diagnostic_id, safe_remote_detail
from skillmind.integrations.mcp_schema import validate_schema, validate_value

PROFILE = "mcp-tools/v1"
MAX_ARGUMENT_BYTES = 65_536


class McpResultError(ValueError):
    """標準の工具失敗を、SKM 相関 ID と安全な遠端データ付きで保持する。"""

    def __init__(
        self,
        reason: str,
        *,
        diagnostic_id: str | None = None,
        remote_detail: dict[str, Any] | None = None,
    ) -> None:
        """例外本文は固定分類のみ。診断本文は認可後に別途投影する。"""
        super().__init__(reason)
        self.reason = reason
        self.local_diagnostic_id = local_diagnostic_id(diagnostic_id)
        self.remote_detail = remote_detail or {}


def digest(value: Any) -> str:
    """契約・観測の canonical JSON を原形のまま識別する。"""
    return "sha256:" + sha256_hex(canonical_json(value))


@lru_cache(maxsize=16)
def _normalized_catalog(encoded: str) -> dict[str, Any]:
    """サービスと Schema の snapshot を検証し、注釈から実行権を生成しない。"""
    value = json.loads(encoded)
    if not isinstance(value, dict) or set(value) != {"server", "tools"}:
        raise ValueError("MCP tool catalog is invalid")
    if len(canonical_json(value).encode()) > 262_144:
        raise ValueError("MCP tool catalog exceeds its limit")
    server, entries = value["server"], value["tools"]
    if (
        not isinstance(server, dict)
        or set(server) != {"name", "version"}
        or any(not isinstance(v, str) or not 1 <= len(v) <= 200 for v in server.values())
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= 100
    ):
        raise ValueError("MCP server or tools are invalid")
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"read_only_hint"} != {
            "name",
            "description",
            "input_schema",
            "output_schema",
        }:
            raise ValueError("MCP tool descriptor is invalid")
        if "read_only_hint" in entry and type(entry["read_only_hint"]) is not bool:
            raise ValueError("MCP read-only annotation must be boolean")
        name = entry["name"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", name)
            or name in names
        ):
            raise ValueError("MCP tool name is invalid or duplicated")
        names.add(name)
        if not isinstance(entry["description"], str) or len(entry["description"]) > 4000:
            raise ValueError("MCP tool description exceeds its limit")
        validate_schema(entry["input_schema"])
        if entry["output_schema"] is not None:
            validate_schema(entry["output_schema"])
    return {"server": dict(server), "tools": sorted(entries, key=lambda item: item["name"])}


def _catalog(value: Any) -> dict[str, Any]:
    """契約の内容だけを有界に cache し、入力 dict の後続変更を検出する。"""
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > 262_144:
        raise ValueError("MCP tool catalog exceeds its limit")
    return _normalized_catalog(encoded)


def normalize_catalog(value: Any) -> dict[str, Any]:
    """公開する snapshot は私有 cache と分離し、呼出し側の変更を共有しない。"""
    return deepcopy(_catalog(value))


def configured_tool(
    config: Mapping[str, Any], scope: Mapping[str, Any], name: str
) -> dict[str, Any]:
    """単一要求では対象工具だけを解決する。現在の権限は毎回検証する。"""
    if config.get("tool_profile") != PROFILE or name not in scope.get("tool_names", []):
        raise ValueError("MCP tool is outside the configured scope")
    if tool_access(config, name) not in {"read", "call"}:
        raise ValueError("MCP tool permission is missing")
    for entry in _catalog(config.get("tool_catalog"))["tools"]:
        if entry["name"] == name:
            return deepcopy(entry)
    raise ValueError("MCP tool is missing from the frozen catalog")


def configured_tools(config: Mapping[str, Any], scope: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一覧公開だけ全 scope を一回走査し、工具数ごとの全 catalog 再検証を避ける。"""
    if config.get("tool_profile") != PROFILE:
        raise ValueError("MCP tool profile is invalid")
    by_name = {entry["name"]: entry for entry in _catalog(config.get("tool_catalog"))["tools"]}
    result = []
    for name in scope.get("tool_names", []):
        if name not in by_name or tool_access(config, name) not in {"read", "call"}:
            raise ValueError("MCP tool permission or contract is missing")
        result.append(deepcopy(by_name[name]))
    return result


def tool_access(config: Mapping[str, Any], name: str) -> str | None:
    """遠端の readOnlyHint は認可に使用しない。"""
    permissions = config.get("tool_permissions", {})
    return permissions.get(name) if isinstance(permissions, dict) else None


def validate_arguments(name: str, arguments: Any, *, proposal: bool = False) -> dict[str, Any]:
    """具体的な引数は保存 Schema に委ね、共通の大きさだけを制限する。"""
    if (
        not isinstance(arguments, dict)
        or len(canonical_json(arguments).encode()) > MAX_ARGUMENT_BYTES
    ):
        raise ValueError("MCP arguments are invalid or too large")
    frozen: dict[str, Any] = json.loads(canonical_json(arguments))
    return frozen


def validate_tool_value(schema: dict[str, Any], value: Any) -> None:
    """外部参照のない小さい Schema で呼出し入力・structured output を照合する。"""
    validate_value(schema, value)


def parse_result(value: Mapping[str, Any], *, credential: str | None = None) -> dict[str, Any]:
    """構造化内容を優先し、その他の内容も取得済みデータとして返す。"""
    if value.get("is_error") is True:
        content = value.get("content")
        # 診断 ID の名前・括弧形式を推測せず、標準 content と structuredContent を保持する。
        detail = {
            "content": [
                item
                for item in content[:8]
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if isinstance(content, list)
            else [],
            "structured_content": value.get("structured_content"),
        }
        raise McpResultError(
            "remote_tool_error",
            diagnostic_id=value.get("local_diagnostic_id"),
            remote_detail=safe_remote_detail(detail, credential=credential),
        )
    if value.get("is_error") is not False:
        raise McpResultError("invalid_response")
    if len(canonical_json(value).encode()) > 1_048_576:
        raise ValueError("MCP result exceeds its limit")
    structured = value.get("structured_content")
    if isinstance(structured, dict):
        return structured
    content = value.get("content")
    if not isinstance(content, list) or len(content) > 100:
        raise McpResultError("invalid_response")
    if len(content) == 1 and isinstance(content[0], dict) and content[0].get("type") == "text":
        try:
            decoded = json.loads(content[0]["text"])
            if isinstance(decoded, dict):
                return decoded
        except (ValueError, TypeError, KeyError):
            pass
    # URI・画像・添付はその場で取得せず、返された内容だけを証拠にする。
    return {"content": content}
