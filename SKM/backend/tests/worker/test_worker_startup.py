"""外部接続なしで、本番 constructor へ準備設定が届くことを検証する。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from arq.worker import Function

from skillmind.agent.database_provider import DatabaseReadProvider
from skillmind.agent.engine import ClaudeAgentSdkEngine, RunMcpRuntime
from skillmind.agent.evidence import PostgresToolAuditWriter
from skillmind.agent.mcp_provider import McpReadProvider
from skillmind.agent.mcp_source import StreamableHttpMcpSource
from skillmind.agent.postgres_source import PostgresDatabaseSource
from skillmind.agent.result_references import PostgresEffectSummaryLookup
from skillmind.agent.result_validation import PostgresArtifactLookup, ResultValidator
from skillmind.agent.subagent_provider import SubagentDispatchProvider
from skillmind.agent.workspace_materializer import WorkspaceMaterializer
from skillmind.core.settings import Settings
from skillmind.runs.domain import LeaseValidationError
from skillmind.runs.repository_inputs import PostgresInputSnapshotStore
from skillmind.worker import settings as worker
from skillmind.worker.executor import AgentRunExecutor
from skillmind.worker.tool_authority import bind_tool_authority, require_tool_authority
from tests.worker.test_agent_run_executor import _claimed, _context


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred_enabled", [False, True])
async def test_startup_injects_required_receipt_and_preparation_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deferred_enabled: bool,
) -> None:
    """呼出しを記録しつつ実 constructor を通し、必須依存の渡し忘れを隠さない。"""

    settings = Settings(  # type: ignore[call-arg]  # BaseSettings の runtime-only 引数。
        _env_file=None,
        contracts_dir=Path(__file__).resolve().parents[3] / "contracts",
        run_workspace_root=tmp_path / "runs",
        run_preparation_timeout_seconds=123,
        workspace_materialize_total_max_bytes=7_654_321,
        workspace_materialize_total_max_files=432,
        managed_secret_kek=None,
        deferred_features_enabled=deferred_enabled,
    )
    sessions = MagicMock()
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "configure_logging", lambda _: None)
    monkeypatch.setattr(worker, "create_database_engine", MagicMock())
    monkeypatch.setattr(worker, "create_session_factory", lambda _: sessions)
    monkeypatch.setattr(worker, "create_file_storage", MagicMock())
    monkeypatch.setattr(
        worker,
        "build_skill_interpreter",
        MagicMock(
            return_value=(
                MagicMock(),
                MagicMock(),
                MagicMock(),
                "test-model",
            )
        ),
    )
    materializer = MagicMock(wraps=WorkspaceMaterializer)
    executor = MagicMock(wraps=AgentRunExecutor)
    validator = MagicMock(wraps=ResultValidator)
    subagent = MagicMock(wraps=SubagentDispatchProvider)
    engine = MagicMock(wraps=ClaudeAgentSdkEngine)
    audit_writer = MagicMock(wraps=PostgresToolAuditWriter)
    registry = MagicMock(wraps=worker.create_run_tool_registry)
    monkeypatch.setattr(worker, "create_run_tool_registry", registry)
    monkeypatch.setattr(worker, "WorkspaceMaterializer", materializer)
    monkeypatch.setattr(worker, "AgentRunExecutor", executor)
    monkeypatch.setattr(worker, "ResultValidator", validator)
    monkeypatch.setattr(worker, "SubagentDispatchProvider", subagent)
    monkeypatch.setattr(worker, "ClaudeAgentSdkEngine", engine)
    monkeypatch.setattr(worker, "PostgresToolAuditWriter", audit_writer)
    context = {"redis": MagicMock()}

    await worker.startup(context)

    postgres = registry.call_args.kwargs["database_provider"]
    assert isinstance(postgres, DatabaseReadProvider)
    assert isinstance(postgres._source, PostgresDatabaseSource)
    mcp = registry.call_args.kwargs["mcp_provider"]
    assert isinstance(mcp, McpReadProvider)
    assert isinstance(mcp._source, StreamableHttpMcpSource)
    assert registry.call_args.kwargs["deferred_features_enabled"] is deferred_enabled
    assert subagent.called is deferred_enabled
    assert ("effect_executor" in context) is deferred_enabled
    assert context["run_service"]._deferred_features_enabled is deferred_enabled
    assert (
        executor.call_args.kwargs["context_builder"]._deferred_features_enabled
        is deferred_enabled
    )

    values = materializer.call_args.kwargs
    assert isinstance(values["input_snapshots"], PostgresInputSnapshotStore)
    assert values["max_total_bytes"] == 7_654_321 and values["max_total_files"] == 432
    assert executor.call_args.kwargs["preparation_timeout_seconds"] == 123
    assert isinstance(context["run_executor"], AgentRunExecutor)
    assert isinstance(validator.call_args.kwargs["effect_lookup"], PostgresEffectSummaryLookup)
    assert isinstance(validator.call_args.kwargs["artifact_lookup"], PostgresArtifactLookup)
    assert isinstance(executor.call_args.kwargs["result_validator"], ResultValidator)
    if deferred_enabled:
        assert (
            executor.call_args.kwargs["result_validator"]
            is subagent.call_args.kwargs["result_validator"]
        )
    factory = engine.call_args.kwargs["mcp_server_factory"]
    claimed = _claimed()
    run_context = _context(claimed, tmp_path, 1)
    with pytest.raises(LeaseValidationError):
        factory(run_context)
    audit_writer.assert_not_called()
    with bind_tool_authority(claimed):
        authority = require_tool_authority(run_context)
        runtime = factory(run_context)
        assert isinstance(runtime, RunMcpRuntime)
        assert audit_writer.call_args.kwargs["claimed_run"] is claimed
        callback = audit_writer.call_args.kwargs["authority_check"]
        assert callback.__self__ is authority
        callback()
    with pytest.raises(LeaseValidationError):
        callback()
    with pytest.raises(LeaseValidationError):
        factory(run_context)
    sessions.assert_not_called()


def test_only_run_job_reserves_additional_preparation_time() -> None:
    """準備を増やしてモデル/終態化の余白を削らず、別 job の上限も変更しない。"""

    registered = [item for item in worker.WorkerSettings.functions if isinstance(item, Function)]
    assert len(registered) == 1
    run_job = registered[0]
    assert run_job.name == "execute_run" and run_job.coroutine is worker.execute_run
    assert run_job.timeout_s == 1200 + worker._settings.run_preparation_timeout_seconds
    assert worker.WorkerSettings.job_timeout == 1200
