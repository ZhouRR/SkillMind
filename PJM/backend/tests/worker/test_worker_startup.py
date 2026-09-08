"""外部接続なしで、本番 constructor へ準備設定が届くことを検証する。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from arq.worker import Function

from projectmind.agent.workspace_materializer import WorkspaceMaterializer
from projectmind.core.settings import Settings
from projectmind.runs.repository_inputs import PostgresInputSnapshotStore
from projectmind.worker import settings as worker
from projectmind.worker.executor import AgentRunExecutor


@pytest.mark.asyncio
async def test_startup_injects_required_receipt_and_preparation_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """呼出しを記録しつつ実 constructor を通し、必須依存の渡し忘れを隠さない。"""

    settings = Settings(
        _env_file=None,
        contracts_dir=Path(__file__).resolve().parents[3] / "contracts",
        run_workspace_root=tmp_path / "runs",
        run_preparation_timeout_seconds=123,
        workspace_materialize_total_max_bytes=7_654_321,
        workspace_materialize_total_max_files=432,
        managed_secret_kek=None,
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
    monkeypatch.setattr(worker, "WorkspaceMaterializer", materializer)
    monkeypatch.setattr(worker, "AgentRunExecutor", executor)
    context = {"redis": MagicMock()}

    await worker.startup(context)

    values = materializer.call_args.kwargs
    assert isinstance(values["input_snapshots"], PostgresInputSnapshotStore)
    assert values["max_total_bytes"] == 7_654_321 and values["max_total_files"] == 432
    assert executor.call_args.kwargs["preparation_timeout_seconds"] == 123
    assert isinstance(context["run_executor"], AgentRunExecutor)
    sessions.assert_not_called()


def test_only_run_job_reserves_additional_preparation_time() -> None:
    """準備を増やしてモデル/終態化の余白を削らず、別 job の上限も変更しない。"""

    registered = [item for item in worker.WorkerSettings.functions if isinstance(item, Function)]
    assert len(registered) == 1
    run_job = registered[0]
    assert run_job.name == "execute_run" and run_job.coroutine is worker.execute_run
    assert run_job.timeout_s == 1200 + worker._settings.run_preparation_timeout_seconds
    assert worker.WorkerSettings.job_timeout == 1200
