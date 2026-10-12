"""選択資源の実権限から Run の Tool と操作を決定する。モデル宣言は使わない。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from skillmind.documents.library import DOCUMENT_LIBRARY_PROVIDER, DOCUMENT_WRITE_CAPABILITY
from skillmind.documents.snapshot import DOCUMENT_CAPABILITIES, DOCUMENT_PROVIDER
from skillmind.effects.operation_policy import WRITE_OPERATIONS
from skillmind.skills.execution import DEFAULT_TOOLS


def resource_capabilities(source: Mapping[str, Any]) -> tuple[str, ...]:
    """Integration の凍結能力、または正規の Project 文書資源だけを読む。"""
    if source.get("provider") == DOCUMENT_LIBRARY_PROVIDER:
        return (DOCUMENT_WRITE_CAPABILITY,)
    if source.get("provider") == DOCUMENT_PROVIDER:
        return DOCUMENT_CAPABILITIES
    values = source.get("capabilities")
    if not isinstance(values, list) or not values or any(
        not isinstance(value, str) for value in values
    ):
        raise ValueError("Selected resource capabilities are missing")
    capabilities = set(values)
    # 同じ資源を使う補助能力を決定的に足す。追加の接続/資格は導入しない。
    if source.get("provider") == "git" and "repository.read/v1" in capabilities:
        capabilities.add("repository.workspace/v1")
    if source.get("provider") == "mcp" and "mcp.query/v1" in capabilities:
        capabilities.add("mcp.download/v1")
    return tuple(sorted(capabilities))


def resource_operations(
    sources: Mapping[str, Any], *, allowed_capabilities: set[str] | frozenset[str],
) -> list[dict[str, str]]:
    """選択接続の範囲と配備上限を超えない外部操作だけを固定する。"""
    result: list[dict[str, str]] = []
    for key, source in sorted(sources.items()):
        if not isinstance(source, Mapping):
            raise ValueError("Selected resource is invalid")
        scope = source.get("scope", {})
        if not isinstance(scope, Mapping):
            raise ValueError("Selected resource scope is invalid")
        for capability in resource_capabilities(source):
            if capability not in allowed_capabilities:
                continue
            for operation in WRITE_OPERATIONS.get(capability, ()):
                if capability == "database.execute/v1" and operation not in scope.get(
                    "statements", ()
                ):
                    continue
                result.append({
                    "resource_key": key, "capability_version": capability, "operation": operation,
                })
    return result


def default_capabilities() -> frozenset[str]:
    """Run 内の通用処理と Project 文書読取を常備し、外部書込権は含めない。"""
    return frozenset(DEFAULT_TOOLS | set(DOCUMENT_CAPABILITIES))
