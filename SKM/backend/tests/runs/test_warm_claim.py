"""暖機続行の認領でも原 Proposal/Approval/Attempt 関係を SQL lock 後に要求する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from skillmind.db.models import AgentSession, ChangeApproval, ChangeProposal, EffectExecution
from skillmind.runs.repository import RunRepository
from sqlalchemy.ext.asyncio import AsyncSession
from tests.runs.test_run_repository import create_queued_run, create_segment
from tests.worker.test_agent_run_executor import _claimed


def case():
    """原実行だけに一致する確定した自動批准チェーンを返す。"""
    run = create_queued_run()
    previous = replace(
        _claimed(),
        run_id=run.id,
        project_id=run.project_id,
        actor_id=UUID(run.permission_snapshot_json["actor_id"]),
        run_segment_id=uuid4(),
    )
    segment = create_segment(run)
    segment.segment_no = previous.segment_no + 1
    segment.continuation_mode = "RESUME"
    segment.trigger_type = "APPROVAL_RESPONSE"
    segment.parent_agent_session_id, segment.trigger_ref = uuid4(), uuid4()
    parent = SimpleNamespace(
        id=segment.parent_agent_session_id,
        run_id=run.id,
        run_attempt_id=previous.run_attempt_id,
        run_segment_id=previous.run_segment_id,
        sdk_session_id=str(uuid4()),
    )
    proposal = SimpleNamespace(
        id=uuid4(),
        run_id=run.id,
        project_id=run.project_id,
        run_segment_id=previous.run_segment_id,
        run_attempt_id=previous.run_attempt_id,
        agent_session_id=parent.id,
        status="APPLIED",
        checksum="hash",
        version=1,
    )
    approval = SimpleNamespace(
        id=uuid4(),
        run_id=run.id,
        proposal_id=proposal.id,
        decision="APPROVED",
        source="RUN_START",
        proposal_checksum="hash",
        proposal_version=1,
    )
    effect = SimpleNamespace(
        id=segment.trigger_ref,
        run_id=run.id,
        proposal_id=proposal.id,
        approval_id=approval.id,
        status="APPLIED",
        error_json=None,
    )
    session = MagicMock(spec=AsyncSession)
    rows = [MagicMock(), MagicMock()]
    rows[0].one_or_none.return_value = run
    rows[1].one_or_none.return_value = segment
    session.scalars = AsyncMock(side_effect=rows)
    session.scalar = AsyncMock(side_effect=[None, 1, 1])
    objects = {
        AgentSession: parent,
        ChangeProposal: proposal,
        ChangeApproval: approval,
        EffectExecution: effect,
    }
    session.get = AsyncMock(side_effect=lambda model, key, **kw: objects[model])
    session.flush = AsyncMock()
    return previous, run, segment, parent, proposal, approval, effect, session


async def claim(previous, run, session):
    """元 repository の claim と通常イベント生成を通す。"""
    return await RunRepository(session).claim_for_execution(
        run.id,
        worker_id="warm",
        lease_token="fixture",
        lease_token_hash="a" * 64,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        max_attempts=3,
        expected_previous=previous,
    )


async def test_matching_automatic_effect_claims_new_attempt_not_old_lease():
    """元の Scope/Attempt を更新せず別 Segment に新 lease を発行する。"""
    previous, run, segment, _, _, _, _, session = case()
    value = await claim(previous, run, session)
    assert value is not None
    assert value.run_segment_id == segment.id and value.run_attempt_id != previous.run_attempt_id
    assert value.parent_run_attempt_id == previous.run_attempt_id
    assert run.status == "PREPARING" and session.flush.await_count == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "manual",
        "unknown",
        "failed",
        "other_attempt",
        "other_actor",
        "other_parent",
        "other_checksum",
        "other_project",
        "other_segment",
        "other_effect_proposal",
        "other_effect_approval",
        "other_version",
        "gap",
        "answer",
    ],
)
async def test_unrelated_or_unconfirmed_successor_is_not_claimed(mutation):
    """不一致時は通常 Queue に委ね、暖機が追加の批准/新操作を作らない。"""
    previous, run, segment, parent, proposal, approval, effect, session = case()
    if mutation == "manual":
        approval.source = "USER"
    elif mutation == "unknown":
        effect.error_json = {"code": "effect_result_unknown"}
    elif mutation == "failed":
        effect.status = "FAILED"
    elif mutation == "other_attempt":
        proposal.run_attempt_id = uuid4()
    elif mutation == "other_actor":
        run.permission_snapshot_json["actor_id"] = str(uuid4())
    elif mutation == "other_parent":
        parent.run_attempt_id = uuid4()
    elif mutation == "other_project":
        proposal.project_id = uuid4()
    elif mutation == "other_segment":
        proposal.run_segment_id = uuid4()
    elif mutation == "other_effect_proposal":
        effect.proposal_id = uuid4()
    elif mutation == "other_effect_approval":
        effect.approval_id = uuid4()
    elif mutation == "other_checksum":
        approval.proposal_checksum = "different"
    elif mutation == "other_version":
        approval.proposal_version = 2
    elif mutation == "gap":
        segment.segment_no += 1
    elif mutation == "answer":
        segment.trigger_type = "USER_RESPONSE"
    assert await claim(previous, run, session) is None
    assert run.status == "QUEUED"
    session.add.assert_not_called()
    session.add_all.assert_not_called()
    session.flush.assert_not_awaited()
