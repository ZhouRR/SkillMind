"""Codex の model 固有 Tool 既定値を、platform Gateway 専用の公開 surface に限定する。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from skillmind.agent.runtime_distribution import _stamp
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


# 成功した同梱 model の派生 bytes だけを保持する。認証・remote discovery・client は共有しない。
_BUNDLED_WAIT_SECONDS = 15.0
_CACHE_LIMIT = 32
_CACHE_ENTRY_BYTES = 1024 * 1024
_BundledKey = tuple[str, tuple[int, ...], str, str, str, str]
_BUNDLED_CACHE: OrderedDict[_BundledKey, bytes] = OrderedDict()
_BUNDLED_LOCK = threading.Lock()
_BUNDLED_FLIGHTS: dict[_BundledKey, threading.Event] = {}


def _bundled_key(
    cli: str, version: str, model: str, effort: str,
) -> _BundledKey | None:
    """固定 executable の identity と派生設定を照合し、不明な実行先は cache しない。"""
    path = Path(cli)
    try:
        value = path.lstat()
        if not path.is_absolute() or not stat.S_ISREG(value.st_mode):
            return None
        return (str(path.resolve()), _stamp(value), version, model, effort,
                canonical_json(_PLATFORM_TOOL_PROFILE))
    except OSError:
        return None


def _load_bundled_content(
    cli: str, cwd: Path, environment: Mapping[str, str], model: str, effort: str,
) -> bytes | None:
    """成功した同梱 model だけを派生し、不足時は呼出し元で remote discovery に戻す。"""
    original = _select_model(_read_catalog(cli, cwd, environment, bundled=True), model)
    if original is None or effort not in {
        entry["effort"] for entry in original["supported_reasoning_levels"]
    }:
        return None
    return _model_content(original)


def _bundled_content(
    cli: str, cwd: Path, environment: Mapping[str, str],
    model: str, effort: str, version: str,
) -> bytes | None:
    """同じ key の初回だけを共有し、異なる設定と remote discovery は直列化しない。"""
    key = _bundled_key(cli, version, model, effort)
    if key is None:
        return _load_bundled_content(cli, cwd, environment, model, effort)
    with _BUNDLED_LOCK:
        if key in _BUNDLED_CACHE:
            _BUNDLED_CACHE.move_to_end(key)
            return _BUNDLED_CACHE[key]
        flight = _BUNDLED_FLIGHTS.get(key)
        owner = flight is None and len(_BUNDLED_FLIGHTS) < _CACHE_LIMIT
        if owner:
            flight = threading.Event()
            _BUNDLED_FLIGHTS[key] = flight
    if not owner:
        # 初回の終了待ちは有界。過密・失敗・待機超過は元の discovery へ戻し、拒否しない。
        if flight is not None and flight.wait(_BUNDLED_WAIT_SECONDS):
            if key != _bundled_key(cli, version, model, effort):
                raise CodexCatalogError("codex:model_catalog_cli_changed")
            with _BUNDLED_LOCK:
                if key in _BUNDLED_CACHE:
                    _BUNDLED_CACHE.move_to_end(key)
                    return _BUNDLED_CACHE[key]
        return _load_bundled_content(cli, cwd, environment, model, effort)
    try:
        content = _load_bundled_content(cli, cwd, environment, model, effort)
        if key != _bundled_key(cli, version, model, effort):
            raise CodexCatalogError("codex:model_catalog_cli_changed")
        if content is not None and len(content) <= _CACHE_ENTRY_BYTES:
            with _BUNDLED_LOCK:
                _BUNDLED_CACHE[key] = content
                while len(_BUNDLED_CACHE) > _CACHE_LIMIT:
                    _BUNDLED_CACHE.popitem(last=False)
        return content
    finally:
        with _BUNDLED_LOCK:
            _BUNDLED_FLIGHTS.pop(key).set()


def _model_content(original: Mapping[str, Any]) -> bytes:
    """推論 metadata を保持し、現行の platform Tool profile だけを適用する。"""
    return canonical_json({"models": [{**original, **_PLATFORM_TOOL_PROFILE}]}).encode("utf-8")


def _validate_catalog_file(path: Path, content: bytes) -> None:
    """Cache hit でも生成 file の通常実体・全 bytes を確認し、symlink を追跡しない。"""
    # 他 process が同内容を atomic replace した場合だけ有界に読み直す。
    # 異なる bytes・symlink は待機や修復で隠さず、そのまま拒否する。
    for _ in range(3):
        expected = path.lstat()
        if not stat.S_ISREG(expected.st_mode):
            raise ValueError("Codex platform model catalog changed")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as source:
            if _stamp(os.fstat(source.fileno())) != _stamp(expected):
                continue
            if source.read(len(content) + 1) != content:
                raise ValueError("Codex platform model catalog changed")
            if _stamp(path.lstat()) == _stamp(expected):
                return
    raise ValueError("Codex platform model catalog changed")


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
    cli_version: str = "",
) -> Path:
    """同梱情報で不足する場合だけ公式 discovery を使い、能力を推測せず Tool 制約を重ねる。"""

    content = _bundled_content(cli, cwd, environment, model, effort, cli_version)
    if content is None:
        # 認証に依存する remote 情報は成功時も失敗時も process cache に入れない。
        original = _select_model(_read_catalog(cli, cwd, environment, bundled=False), model)
        if original is None:
            raise CodexCatalogError("codex:model_catalog_model_not_found")
        efforts = {entry["effort"] for entry in original["supported_reasoning_levels"]}
        if effort not in efforts:
            raise CodexCatalogError("codex:model_catalog_effort_unsupported")
        content = _model_content(original)
    return _materialize_catalog(cwd, content)


def _materialize_catalog(cwd: Path, content: bytes) -> Path:
    """Catalog を atomic replace で公開し、競合時も全 bytes を検証する。"""
    path = cwd / f"platform-model-{sha256_hex(content)}.json"
    if path.exists() or path.is_symlink():
        _validate_catalog_file(path, content)
        return path
    # 完成済み read-only file を既存の atomic replace で公開する。
    # 並行準備が先に作った file は再検証し、疑わしい既存実体を上書きしない。
    with tempfile.NamedTemporaryFile(dir=cwd, prefix=".model-", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(content)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    try:
        temporary_path.chmod(0o400)
        if path.exists() or path.is_symlink():
            _validate_catalog_file(path, content)
        else:
            os.replace(temporary_path, path)
            _validate_catalog_file(path, content)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path
