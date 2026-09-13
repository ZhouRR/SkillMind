"""固定 Codex Python SDK と CLI を、設定と資格情報の境界内で起動する。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.models import UnknownNotification

from skillmind.agent.codex_catalog import platform_model_catalog
from skillmind.agent.runtime_distribution import _stamp, _verify_binary

CODEX_SDK_VERSION = "0.154.0"
CODEX_CLI_VERSION = "0.154.0"

# 固定 CLI の機能リストと実 wire 検証に対応する。資源操作は platform MCP だけを使う。
_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "view_image",
    "apps",
    "plugins",
    "browser_use",
    "computer_use",
    "multi_agent",
    "goals",
    "sleep_tool",
    "skill_search",
    "code_mode_host",
    "tool_suggest",
    "hooks",
    "workspace_dependencies",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "image_generation",
    "js_repl",
    "memory_tool",
    "collaboration_modes",
    "request_permissions_tool",
    "guardian_approval",
    "shell_snapshot",
    "code_mode",
    "code_mode_only",
    "memories",
    "external_agent_memory_import",
    "plugin_hooks",
    "default_mode_request_user_input",
    "context_management",
    "tool_search",
    "tool_search_always_defer_mcp_tools",
)
_PROCESS_ENVIRONMENT = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "PATHEXT",
        "COMSPEC",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)


@dataclass(frozen=True, slots=True)
class CodexRuntimeConfiguration:
    """Deployment が選んだ model/effort と専用の device-login 保存先を保持する。"""

    model: str
    effort: str
    home: Path

    def __post_init__(self) -> None:
        """暗黙の model/effort fallback と開発者のログイン領域共有を拒否する。"""

        if not self.model.strip() or self.model != self.model.strip():
            raise ValueError("Codex model must be explicitly configured")
        if self.effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("Unsupported Codex reasoning effort")
        if not self.home.is_absolute() or self.home.resolve() == Path.home() / ".codex":
            raise ValueError("Codex requires a dedicated absolute home")

    @property
    def primary_model(self) -> str:
        """Run と Interpreter で同一の明示 model ID を返す。"""

        return self.model

    def client_config(self, *, mcp: Mapping[str, Any] | None = None) -> CodexConfig:
        """認証 cache だけを共有し、OS 環境・host instructions・builtin Tools を閉じる。"""

        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.home.is_symlink() or (self.home / "config.toml").exists():
            raise ValueError("Codex runtime home must not contain host configuration")
        cwd = self.home / "runtime"
        cwd.mkdir(exist_ok=True, mode=0o700)
        if cwd.is_symlink() or (cwd / ".codex").exists():
            raise ValueError("Codex runtime directory must not contain project configuration")
        environment = {key: "" for key in os.environ}
        environment.update(
            {key: value for key, value in os.environ.items() if key in _PROCESS_ENVIRONMENT}
        )
        # 配備の代理設定を維持し、受控 MCP の loopback 接続は必ず直接到達させる。
        bypass = dict.fromkeys(
            part.strip()
            for value in (environment.get("NO_PROXY", ""), environment.get("no_proxy", ""),
                          "localhost,127.0.0.1,::1")
            for part in value.split(",") if part.strip()
        )
        environment["NO_PROXY"] = environment["no_proxy"] = ",".join(bypass)
        environment.update(
            {"HOME": str(cwd), "USERPROFILE": str(cwd), "CODEX_HOME": str(self.home)}
        )
        cli = pinned_codex_cli()
        catalog = platform_model_catalog(
            cli=cli,
            cwd=cwd,
            environment=environment,
            model=self.model,
            effort=self.effort,
        )
        overrides = [f"features.{feature}=false" for feature in _DISABLED_FEATURES]
        overrides.extend(
            (
                "features.skip_host_skill_discovery=true",
                'web_search="disabled"',
                "project_doc_max_bytes=0",
                'approval_policy="never"',
                'sandbox_mode="read-only"',
                'cli_auth_credentials_store="file"',
                'forced_login_method="chatgpt"',
                'model_provider="openai"',
                f"model={json.dumps(self.model)}",
                f"model_reasoning_effort={json.dumps(self.effort)}",
                f"model_catalog_json={json.dumps(str(catalog))}",
                "mcp_servers={}",
            )
        )
        if mcp is not None:
            # URL/token は呼出しごとの loopback MCP のみ。外部接続設定は受け入れない。
            for key, value in mcp.items():
                overrides.append(f"mcp_servers.skillmind.{key}={json.dumps(value)}")
        return CodexConfig(
            codex_bin=cli,
            cwd=str(cwd),
            env=environment,
            config_overrides=tuple(overrides),
            client_name="skillmind",
            client_title="Skillmind",
            client_version="0.1.0",
        )


def pinned_codex_cli() -> str:
    """SDK と専用 CLI wheel の実 import/version を確認し、PATH fallback を使わない。"""

    import codex_cli_bin
    import openai_codex

    if (
        metadata.version("openai-codex") != CODEX_SDK_VERSION
        or openai_codex.__version__ != CODEX_SDK_VERSION
        or metadata.version("openai-codex-cli-bin") != CODEX_CLI_VERSION
    ):
        raise RuntimeError("Pinned Codex SDK/CLI distribution is unavailable")
    distribution = metadata.distribution("openai-codex-cli-bin")
    sdk = metadata.distribution("openai-codex")
    if (
        Path(str(openai_codex.__file__)).resolve().parent
        != Path(str(sdk.locate_file("openai_codex"))).resolve()
    ):
        raise RuntimeError("Codex SDK import does not match its distribution")
    package = Path(str(codex_cli_bin.__file__)).resolve().parent
    if package != Path(str(distribution.locate_file("codex_cli_bin"))).resolve():
        raise RuntimeError("Codex CLI import does not match its distribution")
    path = package / "bin" / ("codex.exe" if os.name == "nt" else "codex")
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("Pinned Codex CLI executable is unavailable")
    entries = [
        entry
        for entry in distribution.files or ()
        if entry.as_posix() == f"codex_cli_bin/bin/{path.name}"
    ]
    if len(entries) != 1:
        raise RuntimeError("Codex CLI distribution record is unavailable")
    entry = entries[0]
    if (
        entry.hash is None
        or entry.hash.mode != "sha256"
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", entry.hash.value) is None
        or type(entry.size) is not int
        or not 0 < entry.size <= 512 * 1024 * 1024
    ):
        raise RuntimeError("Codex CLI distribution record is invalid")
    _verify_binary(str(path), entry.size, entry.hash.value, _stamp(path.lstat()))
    return str(path)


def deny_native_request(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """SDK の既定 accept handler を使わず、native 実行や権限拡張を常に拒否する。"""

    del params
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        return {"decision": "decline"}
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "turn"}
    if method == "item/tool/requestUserInput":
        return {"answers": {}}
    raise PermissionError("Codex native request is not authorized")


def create_codex_client(config: CodexConfig) -> CodexClient:
    """全呼出しで同じ native deny boundary を適用する。"""

    return CodexClient(config=config, approval_handler=deny_native_request)


async def codex_notifications(
    client: CodexClient, turn_id: str
) -> AsyncGenerator[dict[str, Any], None]:
    """同期 SDK router を専用待機 thread で読み、元通知の型/値を無加工で返す。"""

    try:
        while True:
            notification = await asyncio.to_thread(client.next_turn_notification, turn_id)
            payload = (
                notification.payload.params
                if isinstance(notification.payload, UnknownNotification)
                else notification.payload.model_dump(mode="json", by_alias=True)
            )
            yield {"method": notification.method, "params": payload}
            if notification.method == "turn/completed":
                return
    finally:
        client.unregister_turn_notifications(turn_id)


async def start_codex(client: CodexClient) -> None:
    """Transport を開始して固定 protocol handshake を完了する。"""

    await asyncio.to_thread(client.start)
    await asyncio.to_thread(client.initialize)
