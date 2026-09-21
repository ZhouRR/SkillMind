"""Codex adapter の環境、原 Session、Tool gateway と未対応予算の拒否境界を検証する。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.agent import codex_catalog
from skillmind.agent.codex_engine import CodexAgentSdkEngine
from skillmind.agent.codex_mcp import CodexToolBridge
from skillmind.agent.codex_runtime import CodexRuntimeConfiguration, deny_native_request
from skillmind.agent.domain import AgentEventType, AgentSessionRef, ResumeContext
from skillmind.agent.session_store import TranscriptKey
from skillmind.runs.budget import BudgetUnavailableError
from tests.agent.test_session_store import MemoryTranscriptBackend
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _context, _registry


def test_catalog_changes_tool_routing_without_changing_reasoning_or_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """モデル既定の code-mode-only は限定し、max と文脈長を元 catalog から保持する。"""

    original = {
        "slug": "selected",
        "default_reasoning_level": "high",
        "context_window": 123456,
        "supported_reasoning_levels": [{"effort": "max"}],
        "tool_mode": "code_mode_only",
        "multi_agent_version": "v2",
        "use_responses_lite": True,
    }
    seen = []

    def bundled(command: list[str], **kwargs: object) -> SimpleNamespace:
        """Bundled-only command の引数を捕捉し、network を使わず元 catalog を返す。"""

        seen.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps({"models": [original]}).encode())

    monkeypatch.setattr(codex_catalog.subprocess, "run", bundled)
    path = codex_catalog.platform_model_catalog(
        cli="/fixture/codex",
        cwd=tmp_path,
        environment={},
        model="selected",
        effort="max",
    )
    actual = json.loads(path.read_text())["models"][0]
    for key in ("slug", "default_reasoning_level", "supported_reasoning_levels", "context_window"):
        assert actual[key] == original[key]
    assert actual["tool_mode"] == "direct"
    assert actual["multi_agent_version"] == "disabled"
    assert actual["apply_patch_tool_type"] is None
    assert seen[0][0] == ["/fixture/codex", "debug", "models", "--bundled"]
    with pytest.raises(ValueError, match="effort"):
        codex_catalog.platform_model_catalog(
            cli="/fixture/codex",
            cwd=tmp_path,
            environment={},
            model="selected",
            effort="low",
        )


def test_worker_credentials_are_cleared_from_codex_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """モデルの認証 cache だけを専用 home へ向け、DB/MinIO/Claude の環境値を消す。"""

    for key in (
        "SKILLMIND_DATABASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "SKILLMIND_OBJECT_STORAGE_SECRET_KEY",
    ):
        monkeypatch.setenv(key, "synthetic-private")
    from skillmind.agent import codex_runtime

    monkeypatch.setattr(codex_runtime, "pinned_codex_cli", lambda: "/fixture/codex")
    monkeypatch.setattr(
        codex_runtime, "platform_model_catalog", lambda **_: tmp_path / "model.json"
    )
    config = CodexRuntimeConfiguration("gpt-5.6-terra", "max", tmp_path / "runtime").client_config()
    assert config.env is not None
    assert "synthetic-private" not in config.env.values()
    assert config.env["OPENAI_API_KEY"] == ""
    assert config.env["CODEX_HOME"] == str(tmp_path / "runtime")
    assert 'model_reasoning_effort="max"' in config.config_overrides
    assert 'model="gpt-5.6-terra"' in config.config_overrides


@pytest.mark.parametrize("prefix", ["upper", "lower"])
def test_codex_preserves_deployment_proxy_and_bypasses_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str,
) -> None:
    """代理は SDK 子プロセスへ届き、元 NO_PROXY と本機 MCP の直結を共に保持する。"""

    from skillmind.agent import codex_runtime

    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    for key in (*keys, *(key.lower() for key in keys)):
        monkeypatch.delenv(key, raising=False)
    selected = keys if prefix == "upper" else tuple(key.lower() for key in keys)
    for key in selected:
        monkeypatch.setenv(key, "http://proxy.example.test:8080")
    monkeypatch.setenv("NO_PROXY", "internal.example.test,localhost")
    monkeypatch.setenv("no_proxy", "other.example.test")
    monkeypatch.setenv("SKILLMIND_DATABASE_URL", "synthetic-private")
    monkeypatch.setattr(codex_runtime, "pinned_codex_cli", lambda: "/fixture/codex")
    monkeypatch.setattr(
        codex_runtime, "platform_model_catalog", lambda **_: tmp_path / "model.json"
    )
    config = CodexRuntimeConfiguration("gpt-5.6-terra", "max", tmp_path / "runtime").client_config()
    assert config.env is not None
    for key in selected:
        assert config.env[key] == "http://proxy.example.test:8080"
    assert config.env["NO_PROXY"] == config.env["no_proxy"]
    assert set(config.env["NO_PROXY"].split(",")) == {
        "internal.example.test", "other.example.test", "localhost", "127.0.0.1", "::1",
    }
    assert config.env["SKILLMIND_DATABASE_URL"] == ""
    assert 'model_reasoning_effort="max"' in config.config_overrides


@pytest.mark.parametrize(
    "method", ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"]
)
def test_native_command_and_file_approval_is_always_denied(method: str) -> None:
    """SDK 既定の accept handler に戻らず、モデル由来の許可提案を拒否する。"""

    assert deny_native_request(method, {"decision": "accept"}) == {"decision": "decline"}


async def test_unregistered_invalid_and_stopped_tools_never_reach_provider(tmp_path: Path) -> None:
    """Tool 名/Schema/停止状態のいずれでも、認可済み Provider 実行前に止める。"""

    registry = _registry(CsvIssueProvider())
    context = _context(tmp_path, registry)
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    bridge = CodexToolBridge(context, runtime, on_deferred=AsyncMock())
    bridge.session_id = str(uuid4())
    for name, arguments in [
        ("Bash", {"command": "ignored"}),
        (context.tools[0].sdk_name, {"issue_ref": "TICKET-1"}),
        (
            context.tools[0].sdk_name,
            {"issue_ref": "TICKET-1", "purpose": "read", "project_id": "other"},
        ),
    ]:
        result = await bridge.invoke(name, arguments, str(uuid4()))
        assert result.isError
    bridge.accepting = False
    assert (
        await bridge.invoke(
            context.tools[0].sdk_name, {"issue_ref": "TICKET-1", "purpose": "read"}, str(uuid4())
        )
    ).isError
    assert not writer.completed


@pytest.mark.parametrize("damage", ["capability", "revision"])
async def test_invalid_effect_input_returns_tool_error_then_corrected_proposal_pauses(
    tmp_path: Path, damage: str,
) -> None:
    """実 Bridge は誤った能力を model に返し、保存・停止は修正された要求だけで行う。"""

    from skillmind.agent.contract_store import (
        ContractStore,
    )
    from skillmind.agent.tool_catalog import (
        _change_propose_tool_definition,
    )
    from skillmind.agent.tool_gateway import ToolRegistry
    from tests.agent.test_tool_policy import _database_proposal

    contracts = Path(__file__).resolve().parents[3] / "contracts"
    registry = ToolRegistry((_change_propose_tool_definition(ContractStore(contracts)),))
    tool = registry.resolve_unbound("change.propose/v1", execution_profile="GUIDED")
    context = replace(
        _context(tmp_path, _registry(CsvIssueProvider())), tools=(tool,),
        permission_snapshot={"allowed_capabilities": [tool.capability]},
    )
    writer = MemoryAuditWriter()
    deferred = AsyncMock()
    bridge = CodexToolBridge(
        context, registry.build_gateway_runtime(context, audit_writer=writer),
        on_deferred=deferred,
    )
    bridge.session_id = str(uuid4())
    arguments, *_ = _database_proposal()
    if damage == "capability":
        arguments["capability_version"] = "change.propose/v1"
    else:
        arguments["precondition"]["revision"] = "ABSENT"
    rejected = await bridge.invoke(tool.sdk_name, arguments, "invalid-call")
    assert rejected.isError
    assert (
        "capability_version must name" if damage == "capability" else "absent (lowercase)"
    ) in rejected.content[0].text
    assert bridge.accepting and not bridge.parked.is_set() and bridge.deferred is None
    deferred.assert_not_awaited()
    arguments["capability_version"] = "database.write/v1"
    arguments["precondition"]["revision"] = "absent"
    corrected = await bridge.invoke(tool.sdk_name, arguments, "corrected-call")
    assert not corrected.isError
    assert not bridge.accepting and bridge.parked.is_set()
    assert bridge.deferred is not None
    assert bridge.deferred[0] is AgentEventType.CHANGE_PROPOSED
    assert bridge.deferred[1]["change_proposal_request"] == arguments
    deferred.assert_awaited_once_with(tool.sdk_name, arguments, "corrected-call", bridge.session_id)
    assert not writer.completed


@pytest.mark.parametrize("damage", ["missing", "other_run", "pending", "model"])
async def test_invalid_parent_session_is_rejected_before_runtime_start(
    tmp_path: Path, damage: str
) -> None:
    """元 Session が欠ける/別 Run/未決/別 model なら、技術 retry でも新 model を呼ばない。"""

    registry = _registry(CsvIssueProvider())
    context = replace(_context(tmp_path, registry), model="gpt-5.6-terra")
    parent = AgentSessionRef(
        context.run_id if damage != "other_run" else uuid4(), uuid4(), str(uuid4())
    )
    backend = MemoryTranscriptBackend()
    if damage != "missing":
        entries = [
            {"type": "codex_start", "model": "other" if damage == "model" else context.model}
        ]
        if damage != "pending":
            entries.append({"type": "codex_terminal"})
        await backend.append(
            TranscriptKey(f"codex:{context.run_id}", parent.session_id), tuple(entries)
        )

    def forbidden_runtime(_: object) -> object:
        """認可確認より先の Runtime/Provider 構築を失敗させる。"""

        raise AssertionError("Runtime must not start")

    engine = CodexAgentSdkEngine(
        configuration=CodexRuntimeConfiguration(context.model, "max", tmp_path / "codex"),
        runtime_factory=forbidden_runtime,
        transcript_backend=backend,
    )
    with pytest.raises(ValueError):
        await anext(engine.resume(ResumeContext(context, parent)))


async def test_codex_cannot_bypass_an_unwired_monetary_budget(tmp_path: Path) -> None:
    """ネイティブ金額計量が未接続なら制限を捨てず、モデル開始前に拒否する。"""

    registry = _registry(CsvIssueProvider())
    base = _context(tmp_path, registry)
    context = replace(base, model="gpt-5.6-terra", limits=replace(base.limits, max_budget_usd=1))
    engine = CodexAgentSdkEngine(
        configuration=CodexRuntimeConfiguration(context.model, "max", tmp_path / "codex"),
        runtime_factory=lambda _: pytest.fail("Runtime must not start"),
        transcript_backend=MemoryTranscriptBackend(),
    )
    with pytest.raises(BudgetUnavailableError):
        await anext(engine.execute(context))
