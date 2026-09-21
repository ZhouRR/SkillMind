"""同 Queue job の認領は原批准後のみ、上限内で通常 claim を通す。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from skillmind.core.settings import Settings
from skillmind.worker.executor import AgentRunExecutor
from skillmind.worker.settings import execute_run
from tests.worker.test_agent_run_executor import _claimed


class WarmExecutor(AgentRunExecutor):
    """job wiring を実証する最小 executor。Effect や claim は他 port で検査する。"""

    def __init__(self):
        """実モデルを起動せず job の所有期間を保存する。"""
        self.calls = []
        self.entered = self.closed = False

    @property
    def supports_warm_continuation(self):
        """この fixture だけが継続 scope を実装する。"""
        return True

    @asynccontextmanager
    async def continuation_scope(self, run_id):
        """有限 job 全体でのみ暖機を保持する。"""
        self.entered = True
        try:
            yield
        finally:
            self.closed = True

    async def execute(self, claimed):
        """各 Attempt の独立した claim を記録する。"""
        assert self.entered and not self.closed
        self.calls.append(claimed)


def fixture(count=2, status="APPLIED", wall=30):
    """明示同意の検証は repository に残し、通常 claim の連鎖だけを模擬する。"""
    first = replace(
        _claimed(),
        run_segment_id=uuid4(),
        segment_no=1,
        limits_snapshot_json={"wall_timeout_seconds": wall},
    )
    claims = [
        replace(first, run_attempt_id=uuid4(), run_segment_id=uuid4(), segment_no=n + 1)
        for n in range(count)
    ]
    executor = WarmExecutor()
    service = MagicMock(claim_run=AsyncMock(side_effect=[*claims, None]))
    effect = MagicMock(execute_pending_for_attempt=AsyncMock(return_value=status))
    context = {
        "run_executor": executor,
        "run_service": service,
        "effect_executor": effect,
        "settings": Settings(
            worker_dispatch_enabled=True, database_writes_enabled=True, _env_file=None
        ),
        "worker_id": "test-warm",
    }
    return context, claims


async def test_warm_job_claims_each_successor_with_exact_previous_identity():
    """前の保存済み Effect と一致する後継だけを repository に要求する。"""
    context, claims = fixture()
    result = await execute_run(context, str(claims[0].run_id))
    assert result["status"] == "accepted"
    assert context["run_executor"].calls == claims
    calls = context["run_service"].claim_run.await_args_list
    assert "expected_previous" not in calls[0].kwargs
    assert calls[1].kwargs["expected_previous"] is claims[0]
    assert calls[2].kwargs["expected_previous"] is claims[1]
    assert context["run_executor"].closed


@pytest.mark.parametrize("status", [None, "FAILED", "REQUESTED", "VERIFICATION_FAILED"])
async def test_unconfirmed_or_manual_wait_returns_to_existing_queue(status):
    """待ち・失敗を成功として続行せず、scope を閉じる。"""
    context, claims = fixture(status=status)
    await execute_run(context, str(claims[0].run_id))
    assert context["run_executor"].calls == claims[:1]
    assert context["run_service"].claim_run.await_count == 1
    assert context["run_executor"].closed


async def test_warm_job_has_five_attempt_bound_and_respects_remaining_deadline():
    """上限後は未認領のまま通常 Outbox に残し、予算を延長しない。"""
    context, claims = fixture(count=8)
    await execute_run(context, str(claims[0].run_id))
    assert len(context["run_executor"].calls) == 5
    assert context["run_service"].claim_run.await_count == 5
    context, claims = fixture(wall=5000)
    await execute_run(context, str(claims[0].run_id))
    assert context["run_executor"].calls == claims[:1]
    assert context["run_service"].claim_run.await_count == 1
