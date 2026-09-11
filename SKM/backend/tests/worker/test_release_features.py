"""首版 gate が古い queue、tick、回復と明示 Tool 注入にも適用されることを検証する。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from skillmind.agent.context_builder import ContractStore, create_run_tool_registry
from skillmind.core.settings import Settings
from skillmind.worker.settings import (
    execute_effect,
    execute_run,
    recover_expired_leases,
    relay_outbox,
    trigger_due_schedules,
)
from tests.worker.test_worker_dispatch import CapturingRelay


@pytest.mark.asyncio
async def test_existing_run_job_cannot_claim_after_dispatch_is_disabled():
    """Outbox で既に転送した job も、claim 前に現在の process 設定を確認する。"""
    service, executor = AsyncMock(), AsyncMock()
    result = await execute_run(
        {
            "settings": Settings(_env_file=None, worker_dispatch_enabled=False),
            "run_service": service,
            "run_executor": executor,
        },
        str(uuid4()),
    )
    assert result["status"] == "disabled"
    service.claim_run.assert_not_called()
    executor.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch,deferred", [(False, True), (True, False), (False, False)])
async def test_queued_effect_and_schedule_tick_stop_before_side_effects(dispatch, deferred):
    """実 executor を誤って残しても、首版/dispatch gate を越えて呼び出さない。"""
    effects, schedules = AsyncMock(), AsyncMock()
    context = {
        "settings": Settings(
            _env_file=None, worker_dispatch_enabled=dispatch, deferred_features_enabled=deferred
        ),
        "effect_executor": effects,
        "schedule_service": schedules,
    }
    assert (await execute_effect(context, str(uuid4())))["status"] == "disabled"
    assert (await trigger_due_schedules(context))["status"] == "disabled"
    effects.execute.assert_not_called()
    schedules.run_due_schedules.assert_not_called()


@pytest.mark.asyncio
async def test_readonly_relay_preserves_effect_outbox_and_relays_manual_runs():
    """外部 effect の未決回执は未配信のまま残し、主 Run と通知は継続する。"""
    relay, redis = CapturingRelay(), AsyncMock()
    await relay_outbox(
        {
            "settings": Settings(
                _env_file=None, worker_dispatch_enabled=True, deferred_features_enabled=False
            ),
            "run_executor": Mock(),
            "effect_executor": Mock(),
            "redis": redis,
            "outbox_relay": relay,
        }
    )
    assert relay.topics == frozenset({"run.dispatch.requested/v1", "run.lifecycle.changed/v1"})


@pytest.mark.asyncio
async def test_readonly_recovery_does_not_redispatch_effects_or_resume_proposals():
    """後置の未知実行を取消/完了に偽装せず、主 Run/interaction の回復だけを続ける。"""
    runs, effects = AsyncMock(), AsyncMock()
    runs.recover_expired_attempts.return_value = 2
    runs.recover_expired_interactions.return_value = 3
    report = await recover_expired_leases(
        {
            "settings": Settings(_env_file=None, deferred_features_enabled=False),
            "run_service": runs,
            "effect_service": effects,
            "worker_id": "fixture-worker",
        }
    )
    assert report["recovered"] == 5
    assert report["recovered_effects"] == report["recovered_proposals"] == 0
    effects.recover_expired_effects.assert_not_called()
    effects.recover_expired_proposals.assert_not_called()


@pytest.mark.parametrize("capability", ["change.propose/v1", "subagent.dispatch/v1"])
def test_readonly_registry_omits_deferred_tools_even_when_provider_is_injected(capability):
    """明示注入や過去 permission snapshot は配備の上限を広げない。"""
    registry = create_run_tool_registry(
        ContractStore(Path(__file__).resolve().parents[3] / "contracts"),
        document_source=Mock(),
        subagent_provider=Mock(),
        deferred_features_enabled=False,
    )
    with pytest.raises(LookupError):
        registry.resolve_unbound(capability, execution_profile="SUPERVISED")
    assert (
        registry.resolve_unbound(
            "interaction.request/v1", execution_profile="SUPERVISED"
        ).capability
        == "interaction.request/v1"
    )
