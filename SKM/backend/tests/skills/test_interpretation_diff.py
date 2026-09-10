"""親子 interpretation candidate の構造化 revision diff を検証する。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from skillmind.skills import diff_interpretations

_PARENT_MANIFEST: dict[str, Any] = {
    "identity": {"skill_key": "repository-review"},
    "capabilities": [{"key": "repository.review", "title": "Repo Review"}],
    "tasks": [{"key": "review-file", "capability": "repository.review"}],
    "tools": [{"capability": "repository.read/v1", "required": True}],
    "workflows": [{"key": "review-file-v1", "steps": []}],
    "permissions": {"external_write_policy": "deny", "network_scope": "project_integrations_only"},
    "ui": {"default_view": "repository-review-report", "views": []},
}
_PARENT_REPORT: dict[str, Any] = {
    "compatibility_level": "adapted",
    "confidence": {"tasks": 0.9, "tools": 0.95},
    "diagnostics": [{"severity": "info", "code": "a", "path": "SKILL.md", "line": 1}],
}


def test_diff_detects_changes_across_every_dimension() -> None:
    """task 追加、capability 変更、permission/level/confidence/diagnostic 差分を捉える。"""

    child_manifest = deepcopy(_PARENT_MANIFEST)
    child_manifest["tasks"].append({"key": "summarize", "capability": "repository.review"})
    child_manifest["capabilities"][0]["title"] = "Repository Review"
    child_manifest["permissions"]["network_scope"] = "none"
    child_report = deepcopy(_PARENT_REPORT)
    child_report["compatibility_level"] = "native"
    child_report["confidence"]["tasks"] = 0.8
    child_report["diagnostics"].append(
        {"severity": "warning", "code": "b", "path": None, "line": None}
    )

    diff = diff_interpretations(
        parent_manifest=_PARENT_MANIFEST,
        child_manifest=child_manifest,
        parent_report=_PARENT_REPORT,
        child_report=child_report,
    )

    assert diff["tasks"] == {"added": ["summarize"], "removed": [], "changed": []}
    assert diff["capabilities"]["changed"] == ["repository.review"]
    assert diff["permissions"]["changed"]["network_scope"] == {
        "from": "project_integrations_only",
        "to": "none",
    }
    assert diff["compatibility_level"] == {"from": "adapted", "to": "native"}
    assert diff["confidence"]["changed"]["tasks"] == {"from": 0.9, "to": 0.8}
    assert diff["diagnostics"]["added"] == ["warning|b||"]
    assert diff["has_changes"] is True


def test_diff_of_identical_candidates_reports_no_changes() -> None:
    """同一 candidate 同士は has_changes を False にする。"""

    diff = diff_interpretations(
        parent_manifest=_PARENT_MANIFEST,
        child_manifest=deepcopy(_PARENT_MANIFEST),
        parent_report=_PARENT_REPORT,
        child_report=deepcopy(_PARENT_REPORT),
    )

    assert diff["has_changes"] is False
    assert diff["tasks"] == {"added": [], "removed": [], "changed": []}
    assert diff["compatibility_level"] is None
