"""既存 Effect repository の直接交付と失効境界を検証する（SQL lock は別検証）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from skillmind.db.models import RunAttempt
from skillmind.effects.domain import (
    EffectEvidenceDraft,
    EffectExecutionStatus,
    EffectFailure,
    EffectLeaseValidationError,
    EffectProviderResult,
)
from tests.runs.test_effect_unknown_outcomes import harness


def inline_case():
    """元 Effect と現在 RUNNING の親 Attempt を正確に対応付ける。"""
    h = harness()
    h.run.status = h.segment.status = "RUNNING"
    h.proposal.inline_owner_json = {
        "phase": "ACTIVE",
        "lease_hash": "parent-owner",
        "tool_use_id": "call-1",
    }
    h.rows[RunAttempt] = SimpleNamespace(
        inline_proposal_id=h.proposal.id,
        run_id=h.run.id,
        run_segment_id=h.segment.id,
        status="RUNNING",
        lease_token_hash="parent-owner",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    return h


async def test_applied_effect_does_not_end_attempt_or_schedule_new_segment():
    """正常な短操作は原事実を保存しても Agent lifecycle を中断しない。"""
    h = inline_case()
    data = {"value": "hallo\r"}
    ev = EffectEvidenceDraft("resource", "fixture://operation", {}, data, None, {})
    result = await h.repository.finalize_effect_execution(
        h.claimed,
        result=EffectProviderResult(ev, ev, {"method": "READ_BACK"}, False),
        failure=None,
        duration_ms=1,
    )
    assert result.status.value == "APPLIED"
    assert h.run.status == h.segment.status == "RUNNING"
    h.repository._next_effect_segment.assert_not_called()
    h.session.add.assert_not_called()
    assert result.before_ref and result.after_ref


async def test_unknown_outcome_is_stored_before_native_pause():
    """通信不明を成功にせず、まず原 Effect に保持して SDK 停止後の移管を待つ。"""
    h = inline_case()
    result = await h.repository.finalize_effect_execution(
        h.claimed,
        result=None,
        failure=EffectFailure(EffectExecutionStatus.FAILED, "effect_authority_revoked", False),
        duration_ms=1,
    )
    assert result.error["code"] == "effect_result_unknown"
    assert h.run.status == "RUNNING" and h.segment.status == "RUNNING"
    h.repository._next_effect_segment.assert_not_called()


@pytest.mark.parametrize("change", ["lease", "expired", "status", "run", "segment"])
async def test_inline_cannot_publish_with_stale_parent(change):
    """Effect の lease が残っていても、元 Agent 所有権の喪失を無視しない。"""
    h = inline_case()
    p = h.rows[RunAttempt]
    if change == "lease":
        p.lease_token_hash = "another-owner"
    elif change == "expired":
        p.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "status":
        p.status = "DEFERRED"
    elif change == "run":
        h.run.status = "CANCELLED"
    else:
        h.segment.status = "WAITING"
    with pytest.raises(EffectLeaseValidationError):
        await h.repository._inline_parent_active(h.run, h.segment, h.proposal)


async def test_queue_cannot_borrow_active_parent_for_inline_claim():
    """通常 Queue は active native Tool の原 claim を盗めない。"""
    h = inline_case()
    h.execution.status = "REQUESTED"
    h.execution.lease_expires_at = None
    h.repository._require_proposal_feature = AsyncMock()
    result = await h.repository.claim_effect_execution(
        h.execution.id,
        worker_id="other",
        lease_token="x",
        lease_token_hash_value="y",
        lease_seconds=30,
        max_attempts=3,
    )
    assert result is None
    assert h.execution.status == "REQUESTED"


def receipt_case():
    """本来の回执と可変 checkpoint を用意し、DB I/O だけを置換する。"""
    from unittest.mock import MagicMock

    from skillmind.core.hashing import canonical_json, sha256_hex
    from skillmind.db.models import EffectExecution, Evidence

    h = inline_case()
    parent = h.rows[RunAttempt]
    parent.id = h.proposal.run_attempt_id
    h.segment.checkpoint_json = {
        "summary": "Keep previous facts.",
        "evidence_refs": ["ev_previous"],
    }
    h.proposal.checkpoint_json = {"summary": "Frozen proposal."}
    h.repository._lock_claimed_execution = AsyncMock(return_value=(h.run, h.segment, parent))
    h.repository._validate_claimed_lease = MagicMock()
    h.repository._reject_cancelled_execution = AsyncMock()
    h.execution.status = "APPLIED"
    h.execution.before_ref, h.execution.after_ref = "ev_before_original", "ev_after_original"
    content = {"actual": "hallo\r"}
    h.after = SimpleNamespace(
        tool_call_id=h.execution.tool_call_id,
        metadata_json={"snapshot": content},
        content_hash="sha256:" + sha256_hex(canonical_json(content)),
    )

    def scalar(statement):
        """Effect と Evidence の集合検索へ原行を返す。"""
        entity = statement.column_descriptions[0]["entity"]
        return {EffectExecution: h.execution, Evidence: h.after}[entity]

    h.session.scalar = AsyncMock(side_effect=scalar)
    return h


async def test_inline_receipt_preserves_raw_value_and_real_checkpoint_fields():
    """存在しない checksum 列や不変提案を変更せず、原値を一度の交付へ渡す。"""
    h = receipt_case()
    result = await h.repository.inline_receipt(h.claimed, h.proposal.id)
    assert result["after"] == {"actual": "hallo\r"}
    assert result["before_ref"] == "ev_before_original"
    assert result["after_ref"] == "ev_after_original"
    assert h.segment.checkpoint_json["evidence_refs"] == [
        "ev_previous",
        "ev_before_original",
        "ev_after_original",
    ]
    assert h.segment.checkpoint_json["change_proposal_refs"] == [h.proposal.proposal_ref]
    assert not hasattr(h.segment, "checkpoint_checksum")
    assert h.proposal.checkpoint_json == {"summary": "Frozen proposal."}
    assert h.proposal.inline_owner_json["phase"] == "DELIVERED"
    assert h.rows[RunAttempt].inline_proposal_id is None
    assert h.run.status == h.segment.status == "RUNNING"
    h.repository._next_effect_segment.assert_not_called()


@pytest.mark.parametrize("field", ["before_ref", "after_ref"])
async def test_missing_inline_reference_is_not_delivered_or_defaulted(field):
    """APPLIED 表示だけで欠落した証拠を補完して model を進めない。"""
    h = receipt_case()
    setattr(h.execution, field, None)
    with pytest.raises(EffectLeaseValidationError):
        await h.repository.inline_receipt(h.claimed, h.proposal.id)
    assert h.proposal.inline_owner_json["phase"] == "ACTIVE"
    assert h.rows[RunAttempt].inline_proposal_id == h.proposal.id


async def test_detaching_confirmed_effect_uses_exact_saved_references():
    """応答断線後も元操作だけを通常続行へ渡し、新規 apply を配送しない。"""
    h = receipt_case()
    h.repository._finish_effect_continuation = AsyncMock()
    await h.repository.detach_inline_effect(
        h.run, h.segment, h.rows[RunAttempt], h.proposal, now=datetime.now(UTC)
    )
    args = h.repository._finish_effect_continuation.await_args.kwargs
    assert args["evidence_refs"] == ("ev_before_original", "ev_after_original")
    assert args["effect_result"]["after"] == {"actual": "hallo\r"}
    assert args["execution"] is h.execution and args["failure"] is None
    assert h.proposal.inline_owner_json["phase"] == "DETACHED"
    assert h.rows[RunAttempt].inline_proposal_id is None
    h.session.add.assert_not_called()


async def test_inline_delivery_retains_new_business_facts_and_is_idempotent():
    """原提案で確認した新事実/参照を持続させ、再交付で重複しない。"""
    from copy import deepcopy

    h = receipt_case()
    h.segment.checkpoint_json["confirmed_facts"] = ["Earlier registration completed."]
    h.proposal.checkpoint_json.update(
        confirmed_facts=["Result artifact has been created."],
        artifact_refs=["art_saved_result"],
    )
    original = deepcopy(h.proposal.checkpoint_json)
    first = await h.repository.inline_receipt(h.claimed, h.proposal.id)
    second = await h.repository.inline_receipt(h.claimed, h.proposal.id)
    assert first == second
    assert h.segment.checkpoint_json["confirmed_facts"] == [
        "Earlier registration completed.",
        "Result artifact has been created.",
    ]
    assert h.segment.checkpoint_json["artifact_refs"] == ["art_saved_result"]
    assert h.segment.checkpoint_json["evidence_refs"] == [
        "ev_previous",
        "ev_before_original",
        "ev_after_original",
    ]
    assert h.proposal.checkpoint_json == original
    h.repository._next_effect_segment.assert_not_called()


async def test_active_inline_cannot_use_another_pending_proposal():
    """同じ Run/Attempt でも待機中の原提案と異なる所有権を借用しない。"""
    from uuid import uuid4

    h = inline_case()
    h.rows[RunAttempt].inline_proposal_id = uuid4()
    with pytest.raises(EffectLeaseValidationError):
        await h.repository._inline_parent_active(h.run, h.segment, h.proposal)
