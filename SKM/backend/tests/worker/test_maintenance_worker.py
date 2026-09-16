"""保守 Queue の配送先、機能 gate と SDK 非起動を検証する。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from arq.worker import Function
from skillmind.core.settings import Settings
from skillmind.storage import InMemoryFileStorage
from skillmind.worker import settings as worker
from tests.worker.test_worker_dispatch import CapturingRelay


async def test_maintenance_startup_has_no_model_or_effect_executor(monkeypatch):
    """発火・回収には共有 service を使い、Secret 復号・SDK・writer を構築しない。"""
    settings = Settings(
        _env_file=None, contracts_dir=Path(__file__).resolve().parents[3] / "contracts"
    )
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "create_database_engine", MagicMock())
    monkeypatch.setattr(worker, "create_session_factory", MagicMock())
    monkeypatch.setattr(worker, "create_file_storage", lambda _: InMemoryFileStorage())
    for name in (
        "load_secret_cipher",
        "build_skill_interpreter",
        "CodexAgentSdkEngine",
        "ClaudeAgentSdkEngine",
        "create_document_write_source",
        "WorkspaceMaterializer",
    ):
        monkeypatch.setattr(worker, name, MagicMock(side_effect=AssertionError(name)))
    ctx = {}
    await worker.maintenance_startup(ctx)
    assert ctx["maintenance_only"] is True
    assert all(
        key in ctx
        for key in (
            "run_service",
            "effect_service",
            "reconciliation_requests",
            "schedule_service",
            "outbox_relay",
        )
    )
    assert not any(
        key in ctx for key in ("run_executor", "effect_executor", "reconciliation_executor")
    )
    assert ctx["skill_service"]._interpreter is None


@pytest.mark.parametrize(
    "enabled,effects", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_maintenance_routes_only_enabled_work_to_execution_queue(enabled, effects):
    """保守 Redis pool の既定 Queue に業務を誤配送せず、元の明示 Queue と gate を守る。"""
    relay = CapturingRelay()
    redis = MagicMock(publish=AsyncMock(), enqueue_job=AsyncMock())
    ctx = {
        "settings": Settings(
            _env_file=None,
            worker_dispatch_enabled=enabled,
            database_writes_enabled=effects,
            queue_name="fixture:runs",
        ),
        "redis": redis,
        "outbox_relay": relay,
        "maintenance_only": True,
        "skill_service": MagicMock(),
    }
    await worker.relay_outbox(ctx)
    jobs = {call.args[0] for call in redis.enqueue_job.await_args_list}
    expected = {
        "execute_run",
        "execute_interpretation_request_job",
        "execute_reconciliation_request_job",
    }
    if effects:
        expected.add("execute_effect")
    assert jobs == (expected if enabled else set())
    for call in redis.enqueue_job.await_args_list:
        assert call.kwargs["_queue_name"] == "fixture:runs"
        assert call.kwargs["_job_id"]


async def test_old_maintenance_ticks_are_retired_without_replaying_business():
    """旧 Queue の既知 tick は副作用なしに吸収し、新 tick だけ保守 Queue で動かす。"""
    assert worker.WorkerSettings.cron_jobs == ()
    assert worker.WorkerSettings.max_jobs == 1
    assert (
        worker.MaintenanceWorkerSettings.queue_name
        == worker.WorkerSettings.queue_name + ":maintenance"
    )
    assert worker.MaintenanceWorkerSettings.functions == (worker.worker_probe,)
    ticks = {job.name for job in worker.MaintenanceWorkerSettings.cron_jobs}
    old = {
        job.name: job
        for job in worker.WorkerSettings.functions
        if isinstance(job, Function) and job.name.startswith("cron:")
    }
    assert ticks == set(old)
    for job in old.values():
        assert await job.coroutine({}) == {"status": "ignored", "reason": "maintenance_queue_moved"}
