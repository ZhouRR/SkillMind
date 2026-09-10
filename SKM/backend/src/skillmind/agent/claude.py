"""Claude Agent SDK 0.2.110 向けの隔離済み option factory を実装する。"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
from claude_agent_sdk.types import HookInput, HookJSONOutput, SessionStore

from skillmind.agent.domain import RunContext
from skillmind.agent.tool_policy import DENIED_BUILTIN_TOOLS, ToolExecutionPolicy

CLAUDE_AGENT_SDK_VERSION = "0.2.110"
CLAUDE_CODE_CLI_VERSION = "2.1.191"
ToolAuthorizationCallback = Callable[[str, Mapping[str, Any], str, str], Awaitable[None]]
ToolDenialCallback = Callable[[str, Mapping[str, Any], str, str, str], Awaitable[None]]
_AGENT_ENVIRONMENT_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
)
_SAFE_INHERITED_ENVIRONMENT_KEYS = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)


@dataclass(frozen=True, slots=True)
class ClaudeRuntimeConfiguration:
    """Agent subprocess へ明示的に渡してよい環境変数だけを保持する。"""

    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        """明示 allowlist 外の process 情報が subprocess へ渡ることを拒否する。"""

        unknown = set(self.environment) - set(_AGENT_ENVIRONMENT_KEYS)
        if unknown:
            raise ValueError(f"Unsupported Claude runtime environment keys: {sorted(unknown)}")

    @property
    def primary_model(self) -> str | None:
        """Custom endpoint にも使える主 model ID を ANTHROPIC_MODEL から返す。"""

        value = self.environment.get("ANTHROPIC_MODEL")
        if value is None or not value.strip():
            return None
        return value.strip()

    @classmethod
    def from_environ(
        cls,
        *,
        fallback: Mapping[str, str | None] | None = None,
    ) -> ClaudeRuntimeConfiguration:
        """Process environment を優先し、許可済み key だけを抽出する。

        CLI が application と同じ ``.env`` を読む場合だけ ``fallback`` を渡せる。明示的に
        注入された空値も含めて process environment を常に優先し、意図せず資格情報を復活
        させない。
        """

        values: dict[str, str] = {}
        for key in _AGENT_ENVIRONMENT_KEYS:
            value = os.environ.get(key) if key in os.environ else (fallback or {}).get(key)
            if isinstance(value, str) and value.strip():
                values[key] = value.strip()
        return cls(environment=values)


def sanitized_agent_environment(configuration: ClaudeRuntimeConfiguration) -> dict[str, str]:
    """CLI subprocess へ渡す環境を単一の allowlist で構築する。

    SDK は options.env を process environment へ加算するため、Worker/API の DB credential
    などを空値で上書きし、OS 由来の安全な key と明示許可済みの Claude 設定だけを残す。
    Engine 実行と interpreter completion の両経路がこの一箇所を共有する。
    """

    environment = {key: "" for key in os.environ}
    environment.update(
        {key: value for key in _SAFE_INHERITED_ENVIRONMENT_KEYS if (value := os.environ.get(key))}
    )
    environment.update(configuration.environment)
    return environment


def build_claude_agent_options(
    context: RunContext,
    *,
    mcp_server: Any,
    configuration: ClaudeRuntimeConfiguration,
    session_store: SessionStore | None = None,
    on_tool_authorized: ToolAuthorizationCallback | None = None,
    on_tool_denied: ToolDenialCallback | None = None,
    deferred_tool_names: frozenset[str] = frozenset(),
) -> ClaudeAgentOptions:
    """Run snapshot を SDK の最小権限 option に変換する。"""

    configured_model = configuration.primary_model
    if configured_model is not None and configured_model != context.model:
        raise ValueError("Run model does not match ANTHROPIC_MODEL")

    if context.permission_snapshot.get("mode") != "auto_read_only":
        raise ValueError("M0 Claude runtime requires auto_read_only permission mode")
    raw_capabilities = context.permission_snapshot.get("allowed_capabilities")
    if not isinstance(raw_capabilities, list) or not all(
        isinstance(capability, str) for capability in raw_capabilities
    ):
        raise ValueError("Permission snapshot must contain allowed_capabilities")
    policy = ToolExecutionPolicy(
        context.tools,
        allowed_capabilities=frozenset(raw_capabilities),
    )

    async def enforce_tool_boundary(
        input_data: HookInput, _tool_use_id: str | None, _hook_context: Mapping[str, Any]
    ) -> HookJSONOutput:
        """PreToolUse の全呼び出しを Schema と Run 登録範囲で再検証する。"""

        if input_data.get("hook_event_name") != "PreToolUse":
            return {}
        try:
            policy.authorize(
                str(input_data.get("tool_name", "")),
                cast(dict[str, Any], input_data.get("tool_input", {})),
            )
        except (PermissionError, ValueError) as error:
            tool_use_id = input_data.get("tool_use_id") or _tool_use_id
            session_id = input_data.get("session_id")
            if (
                on_tool_denied is not None
                and isinstance(tool_use_id, str)
                and isinstance(session_id, str)
            ):
                # Deny 自体を優先し、監査 backend の情報は Hook response へ出さない。
                with suppress(Exception):
                    await on_tool_denied(
                        str(input_data.get("tool_name", "")),
                        cast(dict[str, Any], input_data.get("tool_input", {})),
                        tool_use_id,
                        session_id,
                        str(error),
                    )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": str(error),
                }
            }
        tool_name = str(input_data.get("tool_name", ""))
        if tool_name in deferred_tool_names:
            # Interaction 等の control Tool は Provider を実行せず、SDK Result に検証済み入力を
            # 引き渡して Worker transaction で待機状態へ確定する。
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "defer",
                    "permissionDecisionReason": "Skillmind requires a durable user interaction",
                }
            }
        if on_tool_authorized is not None:
            tool_use_id = input_data.get("tool_use_id") or _tool_use_id
            session_id = input_data.get("session_id")
            if not isinstance(tool_use_id, str) or not isinstance(session_id, str):
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "Missing Skillmind invocation identity",
                    }
                }
            try:
                await on_tool_authorized(
                    tool_name,
                    cast(dict[str, Any], input_data.get("tool_input", {})),
                    tool_use_id,
                    session_id,
                )
            except Exception:
                # Database や coordinator の内部情報を Agent へ返さず、実行前に閉じる。
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "Skillmind tool audit registration failed",
                    }
                }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "Registered Skillmind read-only tool",
            }
        }

    agent_environment = sanitized_agent_environment(configuration)
    agent_environment["CLAUDE_CONFIG_DIR"] = str(context.workspace.root / "claude-config")

    return ClaudeAgentOptions(
        tools=[],
        skills=[],
        allowed_tools=list(policy.allowed_sdk_names),
        disallowed_tools=sorted(DENIED_BUILTIN_TOOLS),
        mcp_servers={"skillmind": mcp_server},
        strict_mcp_config=True,
        permission_mode="default",
        cwd=context.workspace.cwd,
        env=agent_environment,
        setting_sources=[],
        add_dirs=[],
        plugins=[],
        agents=None,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[enforce_tool_boundary])]},
        include_hook_events=True,
        include_partial_messages=True,
        max_turns=context.limits.max_turns,
        max_budget_usd=context.limits.max_budget_usd,
        model=context.model,
        output_format={"type": "json_schema", "schema": dict(context.result_schema)},
        enable_file_checkpointing=False,
        session_store=session_store,
        session_store_flush="batched",
        load_timeout_ms=60_000,
    )
