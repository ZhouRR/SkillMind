"""Codex の model 固有 Tool 既定値を、platform Gateway 専用の公開 surface に限定する。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from skillmind.core.hashing import canonical_json, sha256_hex

# model/推論/文脈長/価格に関する値は変更しない。モデル固有の code-mode-only と v2 子は
# 通常 feature=false より優先されるため、固定 CLI の公式 catalog に最小の Tool 制約を適用。
_PLATFORM_TOOL_PROFILE: Mapping[str, Any] = {
    "tool_mode": "direct",
    "multi_agent_version": "disabled",
    "use_responses_lite": False,
    "apply_patch_tool_type": None,
    "supports_search_tool": False,
    "include_skills_usage_instructions": False,
    "include_plugin_usage_instructions": False,
    "include_apps_usage_instructions": False,
    "node_repl_disabled": True,
    "experimental_supported_tools": [],
}


def platform_model_catalog(
    *,
    cli: str,
    cwd: Path,
    environment: Mapping[str, str],
    model: str,
    effort: str,
) -> Path:
    """認証/network を使わず同梱 catalog を読み、元の推論能力を維持した派生 JSON を保存する。"""

    completed = subprocess.run(
        [cli, "debug", "models", "--bundled"],
        cwd=cwd,
        env=dict(environment),
        capture_output=True,
        check=True,
        timeout=15,
    )
    catalog = json.loads(completed.stdout)
    candidates = [entry for entry in catalog["models"] if entry.get("slug") == model]
    if len(candidates) != 1:
        raise ValueError("Configured Codex model is absent from the pinned catalog")
    original = candidates[0]
    efforts = {entry["effort"] for entry in original["supported_reasoning_levels"]}
    if effort not in efforts:
        raise ValueError("Configured Codex effort is unsupported by the selected model")
    content = canonical_json({"models": [{**original, **_PLATFORM_TOOL_PROFILE}]}).encode("utf-8")
    path = cwd / f"platform-model-{sha256_hex(content)}.json"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError("Codex platform model catalog changed")
        return path
    # 同時 Interpreter/Run が同じ設定を生成しても、途中の JSON は CLI へ渡さない。
    with tempfile.NamedTemporaryFile(dir=cwd, prefix=".model-", delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.chmod(0o400)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path
