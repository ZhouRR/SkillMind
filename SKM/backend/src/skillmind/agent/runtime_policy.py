"""新規 Run だけに実行指示の版を固定し、旧 Run の続行時に方針を変更しない。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

RUNTIME_POLICY = "skillmind.runtime/v4"
SUPPORTED_RUNTIME_POLICIES = frozenset(
    {"skillmind.runtime/v2", "skillmind.runtime/v3", RUNTIME_POLICY}
)


def uses_modern_runtime(snapshot: Mapping[str, Any]) -> bool:
    """監査投影と後続処理も、同じ対応版の集合で判定する。"""
    value = snapshot.get("runtime_policy")
    return isinstance(value, str) and value in SUPPORTED_RUNTIME_POLICIES


def runtime_policy(snapshot: Mapping[str, Any]) -> str | None:
    """未記録は旧方針、未知の明示版は実行前に拒否する。"""
    value = snapshot.get("runtime_policy")
    if value is not None and not uses_modern_runtime(snapshot):
        raise ValueError("Unsupported frozen runtime policy")
    return value if isinstance(value, str) else None
