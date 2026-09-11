"""Claude Agent SDK adapter の固定 version と最小権限 options を検証する。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from skillmind.agent.claude import ClaudeRuntimeConfiguration, build_claude_agent_options
from skillmind.agent.claude_build import bundled_claude_build
from skillmind.agent.compatibility import probe_claude_agent_sdk
from skillmind.agent.domain import RegisteredTool, RunContext, RunLimits, RunWorkspace
from skillmind.agent.tool_policy import DENIED_BUILTIN_TOOLS

ISSUE_READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["issue_ref", "purpose"],
    "properties": {
        "issue_ref": {"type": "string"},
        "purpose": {"type": "string"},
    },
}


def _run_context(tmp_path: Path) -> RunContext:
    """SDK subprocess を開始しない option test 用 RunContext を返す。"""

    run_id = uuid4()
    root = tmp_path / str(run_id)
    return RunContext(
        run_id=run_id,
        run_attempt_id=uuid4(),
        project_id=uuid4(),
        user_id=uuid4(),
        prompt="合成 Ticket を分析する",
        task_snapshot={"capability": "issue.review"},
        skill_snapshots=({"version": "test-issue-review/v1"},),
        resolved_sources={"issue_source": "redmine"},
        permission_snapshot={
            "mode": "auto_read_only",
            "allowed_capabilities": ["issue.read/v1"],
        },
        workspace=RunWorkspace(
            root=root,
            cwd=root / "workspace",
            input_dir=root / "input",
            output_dir=root / "output",
            temp_dir=root / "temp",
        ),
        limits=RunLimits(max_turns=20, wall_timeout_seconds=900, max_output_bytes=1_048_576),
        result_schema={"type": "object", "additionalProperties": False},
        tools=(
            RegisteredTool(
                capability="issue.read/v1",
                sdk_name="mcp__skillmind__issue_read_v1",
                provider="redmine",
                integration_id=uuid4(),
                input_schema=ISSUE_READ_SCHEMA,
            ),
        ),
        model="claude-sonnet-test",
    )


def test_sdk_probe_matches_pinned_sdk_and_bundled_cli() -> None:
    """SDK upgrade 時に使用 interface と同梱 CLI の drift を即座に検出する。"""

    report = probe_claude_agent_sdk()

    assert report.sdk_version == "0.2.110"
    assert report.cli_version == "2.1.191"
    assert report.interrupt_supported
    assert report.in_process_mcp_supported
    assert report.session_store_protocol_supported
    assert report.bundled_cli_checksum == bundled_claude_build().cli_checksum


def test_options_disable_builtin_tools_and_local_configuration(tmp_path: Path) -> None:
    """本機設定や Claude Code 組み込み Tool が Run へ混入しないことを保証する。"""

    options = build_claude_agent_options(
        _run_context(tmp_path),
        mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
        configuration=ClaudeRuntimeConfiguration(
            environment={"ANTHROPIC_AUTH_TOKEN": "test-token"}
        ),
    )

    assert options.tools == []
    assert options.cli_path == bundled_claude_build().cli_path
    assert options.skills == []
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.permission_mode == "default"
    assert options.allowed_tools == ["mcp__skillmind__issue_read_v1"]
    assert set(options.disallowed_tools) == DENIED_BUILTIN_TOOLS
    assert options.env["ANTHROPIC_AUTH_TOKEN"] == "test-token"
    assert options.env["CLAUDE_CONFIG_DIR"].endswith("claude-config")
    assert options.add_dirs == []
    assert options.enable_file_checkpointing is False


def test_options_attach_session_store_without_file_checkpointing(tmp_path: Path) -> None:
    """PostgreSQL mirror と local transcript が併存し、checkpoint は同時利用しない。"""

    session_store = MagicMock()
    options = build_claude_agent_options(
        _run_context(tmp_path),
        mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
        configuration=ClaudeRuntimeConfiguration(environment={}),
        session_store=session_store,
    )

    assert options.session_store is session_store
    assert options.session_store_flush == "batched"
    assert options.load_timeout_ms == 60_000
    assert options.enable_file_checkpointing is False


@pytest.mark.asyncio
async def test_pre_tool_hook_denies_unregistered_tool(tmp_path: Path) -> None:
    """allowed_tools を迂回した呼び出しも PreToolUse で hard deny する。"""

    options = build_claude_agent_options(
        _run_context(tmp_path),
        mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
        configuration=ClaudeRuntimeConfiguration(environment={}),
    )
    assert options.hooks is not None
    hook = options.hooks["PreToolUse"][0].hooks[0]
    input_data: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "session_id": "session-1",
        "transcript_path": "/tmp/transcript",
        "cwd": str(options.cwd),
        "tool_name": "Bash",
        "tool_input": {"command": "pwd"},
        "tool_use_id": "tool-1",
    }

    result = await hook(input_data, "tool-1", {"signal": None})  # type: ignore[arg-type]

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_pre_tool_hook_registers_authorized_invocation(tmp_path: Path) -> None:
    """MCP handler 実行前に SDK tool_use_id と session ID を audit callback へ渡す。"""

    captured: list[tuple[str, dict[str, Any], str, str]] = []

    async def register(
        tool_name: str,
        arguments: dict[str, Any],
        tool_use_id: str,
        session_id: str,
    ) -> None:
        """Hook から渡された invocation identity を記録する。"""

        captured.append((tool_name, arguments, tool_use_id, session_id))

    options = build_claude_agent_options(
        _run_context(tmp_path),
        mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
        configuration=ClaudeRuntimeConfiguration(environment={}),
        on_tool_authorized=register,
    )
    assert options.hooks is not None
    hook = options.hooks["PreToolUse"][0].hooks[0]
    session_id = str(uuid4())
    input_data: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "transcript_path": "/tmp/transcript",
        "cwd": str(options.cwd),
        "tool_name": "mcp__skillmind__issue_read_v1",
        "tool_input": {"issue_ref": "ISSUE-1", "purpose": "analysis"},
        "tool_use_id": "tool-1",
    }

    result = await hook(input_data, "tool-1", {"signal": None})  # type: ignore[arg-type]

    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert captured == [
        (
            "mcp__skillmind__issue_read_v1",
            {"issue_ref": "ISSUE-1", "purpose": "analysis"},
            "tool-1",
            session_id,
        )
    ]


def test_runtime_environment_does_not_forward_unlisted_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Database credential など Agent に不要な process environment を継承しない。"""

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("SKILLMIND_DATABASE_URL", "must-not-be-forwarded")

    configuration = ClaudeRuntimeConfiguration.from_environ()

    assert configuration.environment["ANTHROPIC_AUTH_TOKEN"] == "test-token"
    assert "SKILLMIND_DATABASE_URL" not in configuration.environment


def test_anthropic_model_is_the_primary_model_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom endpoint の model ID を Skillmind 独自設定で上書きしない。"""

    monkeypatch.setenv("ANTHROPIC_MODEL", "custom-provider/model-a")

    configuration = ClaudeRuntimeConfiguration.from_environ()

    assert configuration.primary_model == "custom-provider/model-a"


def test_options_reject_model_drift_from_anthropic_environment(tmp_path: Path) -> None:
    """Run audit snapshot と SDK subprocess の主 model が分岐する設定を拒否する。"""

    with pytest.raises(ValueError, match="does not match ANTHROPIC_MODEL"):
        build_claude_agent_options(
            _run_context(tmp_path),
            mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
            configuration=ClaudeRuntimeConfiguration(
                environment={"ANTHROPIC_MODEL": "custom-provider/model-a"}
            ),
        )


def test_options_blank_worker_credentials_in_sdk_subprocess_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """SDK が親環境を継承しても Worker credential の値を空で上書きする。"""

    monkeypatch.setenv("SKILLMIND_DATABASE_URL", "must-not-reach-agent")
    options = build_claude_agent_options(
        _run_context(tmp_path),
        mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
        configuration=ClaudeRuntimeConfiguration(environment={}),
    )

    assert options.env["SKILLMIND_DATABASE_URL"] == ""
    assert "must-not-reach-agent" not in options.env.values()


def test_runtime_configuration_rejects_explicit_unlisted_key() -> None:
    """呼び出し側が直接構築した場合も database credential の転送を拒否する。"""

    with pytest.raises(ValueError, match="Unsupported Claude runtime environment"):
        ClaudeRuntimeConfiguration(environment={"SKILLMIND_DATABASE_URL": "secret"})


def test_options_reject_tool_outside_permission_snapshot(tmp_path: Path) -> None:
    """ToolRegistry の誤登録が Run の permission snapshot を拡張しないことを保証する。"""

    context = replace(
        _run_context(tmp_path),
        permission_snapshot={"mode": "auto_read_only", "allowed_capabilities": []},
    )

    with pytest.raises(ValueError, match="absent from permission snapshot"):
        build_claude_agent_options(
            context,
            mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
            configuration=ClaudeRuntimeConfiguration(environment={}),
        )
