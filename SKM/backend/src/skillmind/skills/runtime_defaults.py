"""軽量 RuntimeManifest を実行可能な標準形へ決定的に補完する。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast


def normalize_runtime_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """省略可能な capability、workflow、permission、UI、test を標準値で補完する。

    Source や model の宣言から権限を増やさず、常に read-only の platform policy を
    設定する。同じ入力から同じ JSON を返すため、この結果を checksum と Run
    snapshot の正本にできる。
    """

    normalized = deepcopy(dict(manifest))
    identity = _object(normalized, "identity")
    skill_key = _string(identity.get("skill_key")) or "imported-skill"
    compatibility = _object(normalized, "compatibility")
    compatibility.setdefault("confidence", 0.5)
    compatibility.setdefault("diagnostics", [])

    raw_tasks = normalized.get("tasks")
    tasks = raw_tasks if isinstance(raw_tasks, list) else []
    normalized["tasks"] = tasks
    raw_capabilities = normalized.get("capabilities")
    capabilities = raw_capabilities if isinstance(raw_capabilities, list) else []
    capabilities_mutable = raw_capabilities is None or isinstance(raw_capabilities, list)
    raw_workflows = normalized.get("workflows")
    workflows = raw_workflows if isinstance(raw_workflows, list) else []
    workflows_mutable = raw_workflows is None or isinstance(raw_workflows, list)
    capability_keys = {
        _string(item.get("key")) for item in _object_list(capabilities)
    }
    workflow_keys = {_string(item.get("key")) for item in _object_list(workflows)}

    for raw_task in tasks:
        if not isinstance(raw_task, dict):
            continue
        task = cast(dict[str, Any], raw_task)
        task_key = _string(task.get("key"))
        if not task_key:
            # Task key は最低契約なので生成せず、後段 Schema に拒否させる。
            continue
        if "capability" not in task:
            task["capability"] = f"{skill_key}.{task_key}"
        capability = _string(task.get("capability"))
        task.setdefault("type", "immediate")
        if "workflow" not in task:
            task["workflow"] = f"{task_key}-default"
        workflow = _string(task.get("workflow"))
        task.setdefault("view", "standard")
        if capability and capabilities_mutable and capability not in capability_keys:
            capabilities.append(
                {"key": capability, "title": task_key.replace("-", " ").title()}
            )
            capability_keys.add(capability)
        if workflow and workflows_mutable and workflow not in workflow_keys:
            workflows.append(
                {
                    "key": workflow,
                    "steps": [
                        {"key": "execute", "kind": "agent"},
                        {"key": "validate", "kind": "validator"},
                        {"key": "result", "kind": "artifact"},
                    ],
                }
            )
            workflow_keys.add(workflow)

    if capabilities_mutable:
        normalized["capabilities"] = capabilities
    if workflows_mutable:
        normalized["workflows"] = workflows
    normalized.setdefault("tools", [])
    normalized["permissions"] = {
        "default_tool_policy": "auto",
        "registered_script_policy": "auto",
        "external_write_policy": "deny",
        "write_capabilities": [],
        "network_scope": "project_integrations_only",
    }
    raw_ui = normalized.get("ui")
    if raw_ui is None or isinstance(raw_ui, dict):
        ui = cast(dict[str, Any], raw_ui) if isinstance(raw_ui, dict) else {}
        first_view = next(
            (
                task.get("view")
                for task in tasks
                if isinstance(task, dict) and isinstance(task.get("view"), str)
            ),
            "standard",
        )
        ui.setdefault("default_view", first_view)
        ui.setdefault("views", [])
        ui["frontend_module"] = None
        normalized["ui"] = ui
    normalized.setdefault("tests", [])
    normalized.setdefault("extensions", {})
    return normalized


def _object(parent: dict[str, Any], key: str) -> dict[str, Any]:
    """Object field を返し、欠落または異型なら空 object に置換する。"""

    value = parent.get(key)
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    created: dict[str, Any] = {}
    parent[key] = created
    return created


def _object_list(value: Any) -> list[dict[str, Any]]:
    """Object item だけを defensive copy 済み list として返す。"""

    if not isinstance(value, list):
        return []
    return [cast(dict[str, Any], item) for item in value if isinstance(item, dict)]


def _string(value: Any) -> str:
    """空文字を除いた string、または空文字を返す。"""

    return value if isinstance(value, str) and value else ""
