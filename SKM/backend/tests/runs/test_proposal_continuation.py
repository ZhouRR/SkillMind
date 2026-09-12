"""Proposal の原 trigger・要求・回执の一致と、未確定時の拒否を検証する。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    AgentSession,
    ChangeApproval,
    EffectExecution,
    RunSegment,
    UserInteraction,
)
from skillmind.runs.domain import SessionContinuationMode
from skillmind.runs.proposal_continuation import (
    ProposalContinuationReader,
    ResolvedProposal,
    _resolved_outcome,
)


def _rows():
    """外部接続を含まない既処理 Proposal と同じ trigger の Effect を作る。"""
    proposal = SimpleNamespace(
        id=uuid4(),
        run_id=uuid4(),
        status="APPLIED",
        proposal_ref="cp_synthetic",
        capability_version="database.write/v1",
        checksum="sha256:" + "1" * 64,
    )
    effect = SimpleNamespace(
        id=uuid4(),
        run_id=proposal.run_id,
        proposal_id=proposal.id,
        status="APPLIED",
        before_ref="ev_before",
        after_ref="ev_after",
        error_json=None,
    )
    after = {"row": {"status": "RUNNING"}}
    segment = SimpleNamespace(
        trigger_ref=effect.id,
        trigger_type="APPROVAL_RESPONSE",
        checkpoint_json={
            "effect_result": {
                "effect_execution_id": str(effect.id),
                "proposal_ref": proposal.proposal_ref,
                "capability_version": proposal.capability_version,
                "status": "APPLIED",
                "before_ref": effect.before_ref,
                "after_ref": effect.after_ref,
                "after_content_hash": "sha256:" + sha256_hex(canonical_json(after)),
                "after": after,
                "verification": {"matched": True},
            }
        },
    )
    session = AsyncMock()
    session.get.return_value = effect
    return session, segment, proposal, effect


def test_descriptor_matches_only_original_request_and_session():
    """元 idempotency key が同じでも内容や Session が違えば再開許可にしない。"""
    arguments = {"idempotency_key": "synthetic", "changes": [{"value": 1}]}
    resolved = ResolvedProposal(
        sha256_hex(canonical_json(arguments)), "session", "cp_synthetic", "APPLIED"
    )
    assert resolved.matches(arguments, "session")
    assert not resolved.matches(arguments, "another-session")
    assert not resolved.matches({**arguments, "changes": [{"value": 2}]}, "session")


@pytest.mark.asyncio
async def test_resolves_only_exact_applied_receipt():
    """回读を原 Effect と結び、確認自体は ORM 書込を一切行わない。"""
    session, segment, proposal, effect = _rows()
    assert await _resolved_outcome(session, segment, proposal) == "APPLIED"
    session.get.assert_awaited_once_with(EffectExecution, effect.id)
    session.add.assert_not_called()
    session.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        "run", "proposal", "pending", "unknown", "missing_result", "wrong_result",
        "before_ref", "bad_hash",
    ],
)
async def test_unresolved_or_mismatched_effect_is_rejected(mismatch):
    """状態文字列だけで確定とせず、未知・別対象・欠けた回读を閉じる。"""
    session, segment, proposal, effect = _rows()
    if mismatch == "run":
        effect.run_id = uuid4()
    elif mismatch == "proposal":
        effect.proposal_id = uuid4()
    elif mismatch == "pending":
        effect.status = "APPLYING"
    elif mismatch == "unknown":
        effect.error_json = {"code": "effect_result_unknown"}
    elif mismatch == "missing_result":
        segment.checkpoint_json = {}
    elif mismatch == "wrong_result":
        segment.checkpoint_json["effect_result"]["effect_execution_id"] = str(uuid4())
    elif mismatch == "before_ref":
        segment.checkpoint_json["effect_result"]["before_ref"] = "ev_other_observation"
    else:
        segment.checkpoint_json["effect_result"]["after"]["row"]["status"] = "changed"
    with pytest.raises(ValueError):
        await _resolved_outcome(session, segment, proposal)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["REJECTED", "EXPIRED", "STALE", "FAILED", "VERIFICATION_FAILED"]
)
async def test_non_applied_outcome_does_not_invent_a_success_receipt(outcome):
    """拒否・期限切れ・確認済み失敗は事実通りに返し、成功扱いしない。"""
    session, segment, proposal, effect = _rows()
    segment.checkpoint_json = {}
    if outcome == "REJECTED":
        proposal.status = "REJECTED"
        session.get.return_value = SimpleNamespace(
            run_id=proposal.run_id,
            proposal_id=proposal.id,
            decision="REJECTED",
            proposal_checksum=proposal.checksum,
        )
        expected = ChangeApproval
    elif outcome == "EXPIRED":
        proposal.status = "STALE"
        segment.trigger_type = "APPROVAL_TIMEOUT"
        session.get.return_value = SimpleNamespace(
            run_id=proposal.run_id, change_proposal_id=proposal.id, status="EXPIRED"
        )
        expected = UserInteraction
    else:
        proposal.status = "STALE" if outcome == "STALE" else "FAILED"
        effect.status = outcome
        expected = EffectExecution
    assert await _resolved_outcome(session, segment, proposal) == outcome
    session.get.assert_awaited_once_with(expected, effect.id)


@pytest.mark.asyncio
async def test_reader_checks_current_segment_parent_and_checkpoint():
    """別 Segment/parent/checkpoint を持ち込んでも、原提案の確定記述子を渡さない。"""
    session, segment, proposal, effect = _rows()
    parent = SimpleNamespace(
        id=uuid4(), run_id=proposal.run_id, sdk_session_id=uuid4(), run_segment_id=uuid4()
    )
    segment.run_id = proposal.run_id
    segment.id = uuid4()
    segment.parent_agent_session_id = parent.id
    segment.segment_no = 2
    segment.continuation_mode = "RESUME"
    proposal.request_fingerprint = "2" * 64
    claimed = SimpleNamespace(
        continuation_mode=SessionContinuationMode.RESUME,
        run_segment_id=segment.id,
        parent_agent_session_id=parent.id,
        parent_sdk_session_id=parent.sdk_session_id,
        run_id=proposal.run_id,
        project_id=uuid4(),
        segment_no=2,
        checkpoint_json=segment.checkpoint_json,
    )

    async def get(model, identity):
        """Model と原 ID の組にだけ fixture を返す。"""
        return {
            (RunSegment, segment.id): segment,
            (AgentSession, parent.id): parent,
            (EffectExecution, effect.id): effect,
        }.get((model, identity))

    session.get.side_effect = get
    session.scalars.return_value = Mock(one_or_none=Mock(return_value=proposal))
    factory = Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=session)))
    reader = ProposalContinuationReader(factory)
    resolved = await reader.load(claimed)
    assert resolved == ResolvedProposal(
        "2" * 64, str(parent.sdk_session_id), "cp_synthetic", "APPLIED"
    )
    claimed.checkpoint_json = {"forged": True}
    with pytest.raises(ValueError, match="lineage changed"):
        await reader.load(claimed)
    claimed.continuation_mode = SessionContinuationMode.INITIAL
    assert await reader.load(claimed) is None
