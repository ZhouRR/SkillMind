"""Claude Agent SDK の破壊的変更を model 呼び出しなしで検出する。"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, fields

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    SessionStore,
    create_sdk_mcp_server,
)
from claude_agent_sdk._cli_version import __cli_version__
from claude_agent_sdk._version import __version__

from projectmind.agent.claude import CLAUDE_AGENT_SDK_VERSION, CLAUDE_CODE_CLI_VERSION

_REQUIRED_OPTION_FIELDS = frozenset(
    {
        "tools",
        "allowed_tools",
        "disallowed_tools",
        "mcp_servers",
        "strict_mcp_config",
        "permission_mode",
        "cwd",
        "env",
        "hooks",
        "setting_sources",
        "skills",
        "output_format",
        "max_turns",
        "max_budget_usd",
        "resume",
        "fork_session",
        "session_store",
    }
)


@dataclass(frozen=True, slots=True)
class SdkCompatibilityReport:
    """固定 SDK/CLI と ProjectMind が使用する interface の検証結果。"""

    sdk_version: str
    cli_version: str
    option_fields: int
    interrupt_supported: bool
    in_process_mcp_supported: bool
    session_store_protocol_supported: bool


def probe_claude_agent_sdk() -> SdkCompatibilityReport:
    """Network や認証を使わず、固定 version と必要 symbol を検証する。"""

    if __version__ != CLAUDE_AGENT_SDK_VERSION:
        raise RuntimeError(
            "Unsupported Claude Agent SDK: "
            f"expected={CLAUDE_AGENT_SDK_VERSION}, actual={__version__}"
        )
    if __cli_version__ != CLAUDE_CODE_CLI_VERSION:
        raise RuntimeError(
            f"Unsupported bundled Claude CLI: expected={CLAUDE_CODE_CLI_VERSION}, "
            f"actual={__cli_version__}"
        )

    option_fields = {item.name for item in fields(ClaudeAgentOptions)}
    missing = _REQUIRED_OPTION_FIELDS - option_fields
    if missing:
        raise RuntimeError(f"ClaudeAgentOptions is missing required fields: {sorted(missing)}")

    interrupt_supported = inspect.iscoroutinefunction(ClaudeSDKClient.interrupt)
    if not interrupt_supported:
        raise RuntimeError("ClaudeSDKClient.interrupt is unavailable")

    mcp_parameters = inspect.signature(create_sdk_mcp_server).parameters
    in_process_mcp_supported = {"name", "version", "tools"}.issubset(mcp_parameters)
    if not in_process_mcp_supported:
        raise RuntimeError("create_sdk_mcp_server interface is incompatible")

    session_store_protocol_supported = all(
        callable(getattr(SessionStore, method, None))
        for method in ("append", "load", "list_sessions", "delete", "list_subkeys")
    )
    if not session_store_protocol_supported:
        raise RuntimeError("SessionStore interface is incompatible")

    return SdkCompatibilityReport(
        sdk_version=__version__,
        cli_version=__cli_version__,
        option_fields=len(option_fields),
        interrupt_supported=interrupt_supported,
        in_process_mcp_supported=in_process_mcp_supported,
        session_store_protocol_supported=session_store_protocol_supported,
    )
