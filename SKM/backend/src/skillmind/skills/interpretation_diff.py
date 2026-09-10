"""親子 interpretation candidate の構造化 revision diff を決定的に計算する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from skillmind.core.hashing import canonical_json

# Manifest の list dimension と、要素を突き合わせる安定 key。
_KEYED_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("capabilities", "key"),
    ("tasks", "key"),
    ("tools", "capability"),
    ("workflows", "key"),
)


def diff_interpretations(
    *,
    parent_manifest: Mapping[str, Any],
    child_manifest: Mapping[str, Any],
    parent_report: Mapping[str, Any] | None,
    child_report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Identity/capability/task/tool/workflow/permission/ViewSpec/diagnostic を diff する。"""

    result: dict[str, Any] = {
        "identity": {"changed": _diff_object(
            _object(parent_manifest, "identity"), _object(child_manifest, "identity")
        )},
        "permissions": {"changed": _diff_object(
            _object(parent_manifest, "permissions"), _object(child_manifest, "permissions")
        )},
        "ui": {"changed": _diff_object(
            _object(parent_manifest, "ui"), _object(child_manifest, "ui")
        )},
    }
    for dimension, key in _KEYED_DIMENSIONS:
        result[dimension] = _diff_keyed(
            _sequence(parent_manifest, dimension), _sequence(child_manifest, dimension), key
        )
    result["compatibility_level"] = _diff_scalar(
        _report_value(parent_report, "compatibility_level"),
        _report_value(child_report, "compatibility_level"),
    )
    result["confidence"] = {"changed": _diff_object(
        _object_or_empty(parent_report, "confidence"),
        _object_or_empty(child_report, "confidence"),
    )}
    result["diagnostics"] = _diff_diagnostics(
        _report_sequence(parent_report, "diagnostics"),
        _report_sequence(child_report, "diagnostics"),
    )
    result["has_changes"] = _has_changes(result)
    return result


def _diff_keyed(
    parent_items: Sequence[Any], child_items: Sequence[Any], key: str
) -> dict[str, list[str]]:
    """Key 付き list を added/removed/changed へ分解する。"""

    parent_map = _by_key(parent_items, key)
    child_map = _by_key(child_items, key)
    shared = set(parent_map) & set(child_map)
    return {
        "added": sorted(set(child_map) - set(parent_map)),
        "removed": sorted(set(parent_map) - set(child_map)),
        "changed": sorted(k for k in shared if _canon(parent_map[k]) != _canon(child_map[k])),
    }


def _diff_object(parent_obj: Mapping[str, Any], child_obj: Mapping[str, Any]) -> dict[str, Any]:
    """Object の field 別に from/to の差分だけを返す。"""

    changed: dict[str, Any] = {}
    for field in sorted(set(parent_obj) | set(child_obj)):
        before = parent_obj.get(field)
        after = child_obj.get(field)
        if _canon(before) != _canon(after):
            changed[field] = {"from": before, "to": after}
    return changed


def _diff_scalar(before: Any, after: Any) -> dict[str, Any] | None:
    """スカラー値が変わった場合だけ from/to を返す。"""

    if _canon(before) == _canon(after):
        return None
    return {"from": before, "to": after}


def _diff_diagnostics(
    parent_items: Sequence[Any], child_items: Sequence[Any]
) -> dict[str, list[str]]:
    """Diagnostic を severity/code/path/line の複合 key で added/removed へ分ける。"""

    parent_keys = {_diagnostic_key(item) for item in parent_items if isinstance(item, Mapping)}
    child_keys = {_diagnostic_key(item) for item in child_items if isinstance(item, Mapping)}
    return {
        "added": sorted(child_keys - parent_keys),
        "removed": sorted(parent_keys - child_keys),
    }


def _diagnostic_key(item: Mapping[str, Any]) -> str:
    """Diagnostic を位置と code で安定識別する。None は空文字として扱う。"""

    return "|".join(
        "" if item.get(field) is None else str(item.get(field))
        for field in ("severity", "code", "path", "line")
    )


def _by_key(items: Sequence[Any], key: str) -> dict[str, Any]:
    """List を string key で index 化し、非 object は無視する。"""

    indexed: dict[str, Any] = {}
    for item in items:
        if isinstance(item, Mapping):
            value = item.get(key)
            if isinstance(value, str):
                indexed[value] = dict(item)
    return indexed


def _has_changes(result: Mapping[str, Any]) -> bool:
    """いずれかの dimension に差分があるかを判定する。"""

    for name, value in result.items():
        if name in {"has_changes", "compatibility_level"}:
            continue
        if isinstance(value, Mapping):
            if value.get("changed") and _non_empty(value["changed"]):
                return True
            if value.get("added") or value.get("removed"):
                return True
    return result.get("compatibility_level") is not None


def _non_empty(value: Any) -> bool:
    """Changed 集合が空でないかを判定する。"""

    if isinstance(value, Mapping | list):
        return bool(value)
    return value is not None


def _object(manifest: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Manifest の object dimension を取り出し、欠落時は空 object を返す。"""

    value = manifest.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _object_or_empty(report: Mapping[str, Any] | None, key: str) -> dict[str, Any]:
    """Report の object field を取り出し、欠落時は空 object を返す。"""

    if report is None:
        return {}
    value = report.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(manifest: Mapping[str, Any], key: str) -> list[Any]:
    """Manifest の list dimension を取り出し、欠落時は空 list を返す。"""

    value = manifest.get(key)
    return list(value) if isinstance(value, list) else []


def _report_sequence(report: Mapping[str, Any] | None, key: str) -> list[Any]:
    """Report の list field を取り出し、欠落時は空 list を返す。"""

    if report is None:
        return []
    value = report.get(key)
    return list(value) if isinstance(value, list) else []


def _report_value(report: Mapping[str, Any] | None, key: str) -> Any:
    """Report の scalar field を安全に取り出す。"""

    if report is None:
        return None
    return report.get(key)


def _canon(value: Any) -> str:
    """順序に依存しない比較のため共通実装の canonical JSON へ正規化する。"""

    return canonical_json(value)
