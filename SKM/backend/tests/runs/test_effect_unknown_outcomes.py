"""原 Effect の未知を通常失敗へ落とさず、実 repository の停止/復旧/最終化を検証する。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from skillmind.db.models import EffectExecution, RunEvent, RunSegment, ToolCall
from skillmind.effects.domain import (
    EffectEvidenceDraft,
    EffectExecutionStatus,
    EffectFailure,
    EffectProviderResult,
)
from skillmind.effects.outcomes import effect_failure_record
from skillmind.runs.repository_effects import _effect_tool_error
from tests.runs.effect_authorization_harness import AuthorizationHarness


def harness(*, cancelled=False, capability="database.write/v1"):
    """SQL/lock は double、状態遷移と event/outbox 生成は本番実装を使う。"""
    h = AuthorizationHarness()
    now = datetime.now(UTC)
    h.run.row_version, h.run.started_at, h.run.finished_at = 1, now, None
    h.segment.segment_no = 1
    h.proposal.capability_version = capability
    h.execution.error_json = None
    h.execution.before_ref = h.execution.after_ref = None
    h.execution.verification_json = {}
    h.execution.created_at = h.execution.updated_at = now
    h.execution.executed_at = None
    h.execution.tool_call_id = uuid4()
    h.tool = SimpleNamespace(id=h.execution.tool_call_id)
    h.rows[ToolCall] = h.tool
    h.repository.is_cancellation_requested = AsyncMock(return_value=cancelled)
    h.repository._next_sequence = AsyncMock(return_value=10)
    h.repository._next_effect_segment = MagicMock(side_effect=AssertionError("No new Segment"))
    h.session.scalar = AsyncMock(return_value=uuid4() if cancelled else None)

    def scalars(statement):
        """候補一覧と取鎖後の元 row を同じ identity で返す。"""
        result = MagicMock()
        entity = statement.column_descriptions[0]["entity"]
        result.one.return_value = h.rows[entity]
        result.all.return_value = [h.execution] if entity is EffectExecution else []
        return result

    h.session.scalars = AsyncMock(side_effect=scalars)
    return h


def assert_stopped(h, *, cancelled=False):
    """Effect、Tool、terminal snapshot に未知を残し、次 Segment/dispatch を作らない。"""
    assert h.execution.error_json["code"] == "effect_result_unknown"
    assert h.run.status == ("CANCELLED" if cancelled else "FAILED")
    assert h.run.error_json["code"] == "effect_result_unknown"
    assert h.run.error_json["effect_execution_id"] == str(h.execution.id)
    assert h.tool.error_json["code"] == "effect_result_unknown"
    added = [row for call in h.session.add_all.call_args_list for row in call.args[0]]
    events = [row for row in added if isinstance(row, RunEvent)]
    assert [row.event_type for row in events] == ["EFFECT_FAILED", "RUN_SNAPSHOT"]
    assert events[0].payload_json["error"] == h.execution.error_json
    assert events[-1].payload_json["error"] == h.run.error_json
    assert not any(isinstance(row, RunSegment) for row in added)
    h.repository._next_effect_segment.assert_not_called()
    h.session.add.assert_not_called()


@pytest.mark.parametrize("capability", ["database.write/v1", "document.write/v1"])
@pytest.mark.parametrize("code", ["retry_exhausted", "proposal_expired", "run_cancelled"])
async def test_stopping_before_next_provider_preserves_original_unknown(capability, code):
    """再試行打切りは、過去 attempt の原因を保ったまま主処理を停止する。"""
    cancelled = code == "run_cancelled"
    h = harness(cancelled=cancelled, capability=capability)
    h.execution.status = "REQUESTED"
    h.execution.error_json = effect_failure_record(
        capability=capability,
        attempt_no=1,
        code="document_effect_uncertain",
        retryable=True,
        previous=None,
    )
    await h.repository._close_effect_before_provider(
        run=h.run,
        segment=h.segment,
        proposal=h.proposal,
        execution=h.execution,
        status=EffectExecutionStatus.FAILED,
        code=code,
        now=datetime.now(UTC),
    )
    assert h.execution.error_json["cause_code"] == "document_effect_uncertain"
    assert h.execution.error_json["reason_code"] == code
    assert_stopped(h, cancelled=cancelled)


@pytest.mark.parametrize("reason", ["cancelled", "expired", "exhausted", "retry", "left_waiting"])
async def test_expired_lease_recovery_keeps_unknown_even_without_provider_response(reason):
    """Worker crash/監督停止で failure が返らなくても、claim 済み lease の回収で未知を残す。"""
    h = harness(cancelled=reason == "cancelled")
    now = datetime.now(UTC)
    h.execution.lease_expires_at = now - timedelta(seconds=1)
    if reason == "expired":
        h.proposal.expires_at = now - timedelta(seconds=1)
    if reason == "left_waiting":
        h.run.status = "CANCELLED"
    assert (
        await h.repository.recover_expired_effects(
            now=now,
            limit=5,
            max_attempts=1 if reason == "exhausted" else 3,
        )
        == 1
    )
    assert h.execution.error_json["code"] == "effect_result_unknown"
    if reason == "retry":
        assert h.execution.status == "REQUESTED"
        assert h.run.status == "WAITING_FOR_APPROVAL"
        assert h.tool.error_json["code"] == "effect_result_unknown"
        assert h.session.add.call_count == 1
    elif reason == "left_waiting":
        assert h.run.status == "CANCELLED"
        assert h.tool.error_json["code"] == "effect_result_unknown"
        h.session.add_all.assert_not_called()
        h.session.add.assert_not_called()
    else:
        assert_stopped(h, cancelled=reason == "cancelled")


@pytest.mark.parametrize("retryable,cancelled", [(False, False), (True, True), (True, False)])
async def test_finalize_preserves_unknown_and_only_retries_original_effect(retryable, cancelled):
    """応答未知/書込後失権を通常失敗の続行へ渡さず、許可内 retry だけ元 identity を使う。"""
    h = harness(cancelled=cancelled)
    stored = await h.repository.finalize_effect_execution(
        h.claimed,
        result=None,
        failure=EffectFailure(EffectExecutionStatus.FAILED, "effect_authority_revoked", retryable),
        duration_ms=1,
    )
    assert stored.error["code"] == "effect_result_unknown"
    assert stored.effect_execution_id == h.claimed.effect_execution_id
    if retryable and not cancelled:
        assert stored.status.value == "REQUESTED"
        assert h.run.status == "WAITING_FOR_APPROVAL"
        h.repository._next_effect_segment.assert_not_called()
    else:
        assert_stopped(h, cancelled=cancelled)


async def test_never_claimed_cancellation_does_not_invent_an_unknown_write():
    """まだ一度も Provider claim を渡していない場合、従来の未開始取消を維持する。"""
    h = harness(cancelled=True)
    h.execution.attempt_no = 0
    h.execution.tool_call_id = None
    await h.repository._close_effect_before_provider(
        run=h.run,
        segment=h.segment,
        proposal=h.proposal,
        execution=h.execution,
        status=EffectExecutionStatus.FAILED,
        code="run_cancelled",
        now=datetime.now(UTC),
    )
    assert h.execution.error_json == {"code": "run_cancelled", "retryable": False}
    assert h.run.status == "CANCELLED" and h.run.error_json is None


async def test_original_receipt_success_clears_unknown_and_continues_after_applied():
    """原 Provider の確認済み回执は未確定を解消し、APPLIED Evidence を渡して続行する。"""
    h = harness()
    h.execution.error_json = effect_failure_record(
        capability="database.write/v1",
        attempt_no=1,
        code="database_effect_uncertain",
        retryable=True,
        previous=None,
    )
    h.repository._next_effect_segment = MagicMock(
        return_value=SimpleNamespace(id=uuid4(), segment_no=2)
    )
    evidence = EffectEvidenceDraft("database", "fixture://receipt", {}, {"id": 1}, None, {})
    stored = await h.repository.finalize_effect_execution(
        h.claimed,
        result=EffectProviderResult(evidence, evidence, {"replayed": True}, True),
        failure=None,
        duration_ms=1,
    )
    assert stored.status.value == "APPLIED" and stored.error is None
    assert stored.before_ref and stored.after_ref and h.tool.error_json is None
    assert h.run.status == "QUEUED" and h.run.error_json is None
    assert h.repository._next_effect_segment.call_args.kwargs["outcome"] == "APPLIED"


def test_old_uncertain_code_is_preserved_when_recovery_stops():
    """新 marker のない旧記録も元 cause を保持し、停止理由で履歴を塗り替えない。"""
    record = effect_failure_record(
        capability="database.write/v1",
        attempt_no=2,
        code="retry_exhausted",
        retryable=False,
        previous={"code": "database_effect_uncertain", "retryable": True},
    )
    assert record == {
        "code": "effect_result_unknown",
        "retryable": False,
        "cause_code": "database_effect_uncertain",
        "reason_code": "retry_exhausted",
    }


@pytest.mark.parametrize("capability", ["database.write", "document.write"])
def test_unknown_tool_record_matches_public_error_contract(capability):
    """実保存用 Tool error を公開 schema で検証し、未登録 code を持ち出さない。"""
    schema_path = (
        Path(__file__).resolve().parents[3]
        / "contracts/tools" / capability / "v1/error.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    Draft202012Validator(schema).validate(
        _effect_tool_error(code="effect_result_unknown", retryable=False)
    )
