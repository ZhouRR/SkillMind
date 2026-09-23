"""Codex の model 固有 Tool 既定値を、platform Gateway 専用の公開 surface に限定する。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

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


class CodexCatalogError(ValueError):
    """CLI の本文や資格情報を含まない、モデル準備の固定診断。"""

    def __init__(self, detail: str) -> None:
        """公開可能な分類だけを上位の preparation failure へ渡す。"""
        super().__init__(detail)
        self.detail = detail


def _read_catalog(
    cli: str, cwd: Path, environment: Mapping[str, str], *, bundled: bool,
) -> list[dict[str, Any]]:
    """固定 CLI に discovery を任せ、専用認証・代理設定で有界の読取だけを行う。"""
    command = [cli, "debug", "models"]
    if bundled:
        command.append("--bundled")
    else:
        # --bundled を外すと CLI 自身が認証済み catalog/cache を更新する。推論は開始しない。
        command.extend([
            "-c", 'cli_auth_credentials_store="file"',
            "-c", 'forced_login_method="chatgpt"',
            "-c", 'model_provider="openai"',
        ])
    try:
        completed = subprocess.run(
            command, cwd=cwd, env=dict(environment), capture_output=True,
            check=True, timeout=15 if bundled else 30,
        )
    except subprocess.TimeoutExpired as error:
        raise CodexCatalogError("codex:model_catalog_timeout") from error
    except (OSError, subprocess.CalledProcessError) as error:
        raise CodexCatalogError("codex:model_catalog_unavailable") from error
    try:
        catalog = json.loads(completed.stdout)
    except (ValueError, UnicodeError) as error:
        raise CodexCatalogError("codex:model_catalog_invalid") from error
    if (
        not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list)
        or any(not isinstance(entry, dict) for entry in catalog["models"])
    ):
        raise CodexCatalogError("codex:model_catalog_invalid")
    return cast(list[dict[str, Any]], catalog["models"])


def _select_model(catalog: list[dict[str, Any]], model: str) -> dict[str, Any] | None:
    """別名・類似モデルへの置換をせず、公式の完全一致 entry だけを採用する。"""
    candidates = [entry for entry in catalog if entry.get("slug") == model]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise CodexCatalogError("codex:model_catalog_invalid")
    levels = candidates[0].get("supported_reasoning_levels")
    if not isinstance(levels, list) or not levels or any(
        not isinstance(level, dict) or not isinstance(level.get("effort"), str)
        for level in levels
    ):
        raise CodexCatalogError("codex:model_catalog_invalid")
    return candidates[0]


def platform_model_catalog(
    *,
    cli: str,
    cwd: Path,
    environment: Mapping[str, str],
    model: str,
    effort: str,
) -> Path:
    """同梱情報で不足する場合だけ公式 discovery を使い、能力を推測せず Tool 制約を重ねる。"""

    original = _select_model(_read_catalog(cli, cwd, environment, bundled=True), model)
    if original is None or effort not in {
        entry["effort"] for entry in original["supported_reasoning_levels"]
    }:
        original = _select_model(_read_catalog(cli, cwd, environment, bundled=False), model)
    if original is None:
        raise CodexCatalogError("codex:model_catalog_model_not_found")
    efforts = {entry["effort"] for entry in original["supported_reasoning_levels"]}
    if effort not in efforts:
        raise CodexCatalogError("codex:model_catalog_effort_unsupported")
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
