"""実 service を使わず、永続認領の SQL・原要求・rollback 境界を検証する。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine.interfaces import Dialect

from projectmind.runs.creation_request import TaskRunIntent
from projectmind.schedules.domain import (
    ScheduleClaimLostError,
    ScheduleConflictError,
    ScheduleInvalidError,
    ScheduleOccurrenceConflictError,
    ScheduleOutcome,
    ScheduleStatus,
    ScheduleTriggerResult,
    UpdateScheduleCommand,
)
from projectmind.schedules.repository import _to_record
from tests.schedules.fakes import (
    NOW,
    TOKEN,
    ScheduleDatabase,
    occurrence_row,
    run_for_occurrence,
    schedule_row,
)


def _outcome(db: ScheduleDatabase, *, run: bool = False) -> ScheduleTriggerResult:
    """外部実行を模倣せず、保存対象の結算だけを作る。"""

    run_id = None
    if run:
        created = run_for_occurrence(db.occurrences[0])
        db.runs.append(created)
        run_id = created.id
    return ScheduleTriggerResult(
        db.claim.schedule_id,
        db.claim.occurrence_at,
        ScheduleOutcome.RUN_CREATED if run else ScheduleOutcome.SKIPPED_OVERLAP,
        run_id,
    )


@pytest.mark.asyncio
async def test_claim_persists_original_snapshot_before_advancing_candidate() -> None:
    """次候補を消費しても、失敗後に戻れる元の要求と一件の枠が残る。"""

    db = ScheduleDatabase()
    record = _to_record(db.schedule)
    claim = await db.repo.claim_due(record, now=NOW, worker_id="worker", token=TOKEN)
    assert claim is not None
    assert db.schedule.next_run_at == NOW + timedelta(minutes=1)
    assert db.schedule.run_count == 0
    assert db.schedule.row_version == 2
    assert len(db.occurrences) == 1
    assert db.occurrences[0].status == "PENDING"
    assert db.occurrences[0].snapshot_json["request"]["input"] == {"value": 1}
    assert TOKEN not in repr(claim)
    db.schedule.input_json["value"] = 5
    assert claim.input_json == {"value": 1}
    assert await db.repo.claim_due(record, now=NOW, worker_id="other", token="other") is None
    sql = db.statements[0]
    assert "FOR UPDATE" in str(sql)
    assert sql.get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", ["row_version", "configuration_version", "status", "next_run_at"]
)
async def test_candidate_is_rechecked_under_lock(changed: str) -> None:
    """一覧読取後の編集・停止・別 worker 認領を上書きしない。"""

    db = ScheduleDatabase()
    record = _to_record(db.schedule)
    changes = {
        "row_version": 2,
        "configuration_version": 2,
        "status": "PAUSED",
        "next_run_at": NOW + timedelta(minutes=1),
    }
    setattr(db.schedule, changed, changes[changed])
    assert await db.repo.claim_due(record, now=NOW, worker_id="worker", token=TOKEN) is None
    assert not db.occurrences
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_capacity_blocks_claim_and_does_not_starve_due_listing() -> None:
    """試行上限を超えた未決も残枠を占めるが、候補一覧の先頭には居座らない。"""

    db = ScheduleDatabase(claimed=True)
    db.occurrences[0].attempt_count = 3
    assert (
        await db.repo.claim_due(
            _to_record(db.schedule),
            now=NOW + timedelta(minutes=1),
            worker_id="other",
            token="other",
        )
        is None
    )
    assert await db.repo.list_due(now=NOW + timedelta(minutes=1), limit=20) == []
    assert "NOT (EXISTS" in str(db.statements[-1])
    assert db.schedule.run_count == 0


@pytest.mark.asyncio
async def test_legacy_tick_preserves_unverifiable_counts_and_last_run() -> None:
    """旧摘要を零化も監査補造もせず、ERROR で自動発火を閉じる。"""

    db = ScheduleDatabase()
    db.schedule.occurrence_protocol = 0
    db.schedule.run_count = 7
    db.schedule.last_run_id = uuid4()
    previous = db.schedule.last_run_id
    assert (
        await db.repo.claim_due(_to_record(db.schedule), now=NOW, worker_id="worker", token=TOKEN)
        is None
    )
    assert db.schedule.status == "ERROR"
    assert db.schedule.run_count == 7
    assert db.schedule.last_run_id == previous
    assert not db.occurrences


@pytest.mark.asyncio
async def test_recovery_keeps_original_intent_and_fences_previous_generation() -> None:
    """新しい定義・pause に影響されず、元の一件を再認領する。"""

    db = ScheduleDatabase(claimed=True)
    original = db.claim
    payload = deepcopy(db.occurrences[0].snapshot_json)
    db.schedule.input_json = {"replacement": True}
    db.schedule.configuration_version = 2
    db.schedule.status = "PAUSED"
    recovered = await db.repo.claim_recoverable(
        now=NOW + timedelta(seconds=60), limit=20, worker_id="recovery", token="new-token"
    )
    assert len(recovered) == 1
    assert recovered[0].occurrence_id == original.occurrence_id
    assert recovered[0].input_json == original.input_json
    assert recovered[0].lease_generation == 2
    assert db.occurrences[0].snapshot_json == payload
    assert db.schedule.run_count == 0
    with pytest.raises(ScheduleClaimLostError):
        await db.repo.lock_claim(original, now=NOW + timedelta(seconds=61))
    assert (
        await db.repo.lock_claim(recovered[0], now=NOW + timedelta(seconds=61))
    ).claim == recovered[0]
    dialect_factory: Callable[[], Dialect] = PGDialect
    locked_sql = [
        str(statement.compile(dialect=dialect_factory()))
        for statement in db.statements
        if statement._for_update_arg is not None
    ]
    assert "task_schedules" in locked_sql[0]
    assert "task_schedule_occurrences" in locked_sql[1]
    assert "SKIP LOCKED" in locked_sql[0]


@pytest.mark.asyncio
async def test_attempt_limit_does_not_fabricate_failure_or_release_capacity() -> None:
    """基盤障害で尽きた試行から、業務失効を推測しない。"""

    db = ScheduleDatabase(claimed=True)
    db.occurrences[0].attempt_count = 3
    assert (
        await db.repo.claim_recoverable(
            now=NOW + timedelta(minutes=2), limit=20, worker_id="worker", token=TOKEN
        )
        == []
    )
    assert db.occurrences[0].status == "PENDING"
    assert db.occurrences[0].outcome is None
    assert db.schedule.run_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_id", "other"),
        ("lease_token", "other"),
        ("lease_generation", 2),
        ("lease_generation", True),
        ("lease_expires_at", NOW + timedelta(seconds=59)),
    ],
)
async def test_wrong_fence_never_settles(field: str, value: Any) -> None:
    """元要求が同じでも、新しい世代の権利を古い DTO に与えない。"""

    db = ScheduleDatabase(claimed=True)
    claim = replace(db.claim, **{field: value})
    with pytest.raises(ScheduleClaimLostError):
        await db.repo.record_outcome(claim, _outcome(db), now=NOW)
    assert db.occurrences[0].status == "PENDING"
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [True, 1.0])
async def test_saved_snapshot_numeric_corruption_is_not_equal_to_original(corruption: Any) -> None:
    """Python の数値等価ではなく保存 JSON 実値の hash を検証する。"""

    db = ScheduleDatabase(claimed=True)
    claim = db.claim
    db.occurrences[0].snapshot_json["request"]["input"]["value"] = corruption
    with pytest.raises(ScheduleOccurrenceConflictError):
        await db.repo.lock_claim(claim, now=NOW)


@pytest.mark.asyncio
async def test_expiry_after_flush_rolls_back_settlement_and_count() -> None:
    """SETTLED 化した自分の行を理由に期限検証を省略しない。"""

    db = ScheduleDatabase(claimed=True)
    result = _outcome(db, run=True)
    with (
        patch(
            "projectmind.schedules.repository_occurrences._now",
            side_effect=[NOW, NOW, NOW + timedelta(seconds=60)],
        ),
        pytest.raises(ScheduleClaimLostError),
    ):
        async with db.transaction():
            await db.repo.record_outcome(db.claim, result)
    assert db.schedule.run_count == 0
    assert db.occurrences[0].status == "PENDING"
    assert db.occurrences[0].run_id is None


@pytest.mark.asyncio
async def test_run_association_counts_once_and_can_confirm_after_expiry() -> None:
    """B の Run 関連が既に commit 済みなら C の確認で再加算しない。"""

    db = ScheduleDatabase(claimed=True)
    claim = db.claim
    result = _outcome(db, run=True)
    original_hash = db.runs[0].request_hash
    assert await db.repo.record_outcome(claim, result, now=NOW) == result
    version = db.schedule.row_version
    assert await db.repo.record_outcome(claim, result, now=NOW + timedelta(days=1)) == result
    assert db.schedule.run_count == 1
    assert db.schedule.row_version == version
    assert db.runs[0].request_hash == original_hash
    with pytest.raises(ScheduleOccurrenceConflictError):
        await db.repo.record_outcome(claim, replace(result, run_id=uuid4()), now=NOW)


@pytest.mark.asyncio
async def test_unrelated_run_cannot_be_counted_as_original_occurrence() -> None:
    """Run id だけを受け取って無関係な履歴を schedule へ紐付けない。"""

    db = ScheduleDatabase(claimed=True)
    result = replace(_outcome(db, run=True), run_id=uuid4())
    with pytest.raises(ScheduleOccurrenceConflictError):
        await db.repo.record_outcome(db.claim, result, now=NOW)
    assert db.schedule.run_count == 0
    assert db.occurrences[0].status == "PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["PAUSED", "ARCHIVED", "definition", "pause-resume"])
async def test_old_settlement_does_not_overwrite_user_control(changed: str) -> None:
    """認領後の停止は元の一件を撤回しないが、旧結果は新しい制御を上書きしない。"""

    db = ScheduleDatabase(claimed=True)
    claim = db.claim
    if changed == "definition":
        db.schedule.configuration_version += 1
        db.schedule.input_json = {"new": True}
    elif changed != "pause-resume":
        db.schedule.status = changed
        db.schedule.next_run_at = None
    db.schedule.row_version += 1
    result = replace(_outcome(db), outcome=ScheduleOutcome.FAILED_PRECONDITION)
    await db.repo.record_outcome(claim, result, now=NOW)
    assert db.schedule.status == (changed if changed in {"PAUSED", "ARCHIVED"} else "ACTIVE")
    assert db.occurrences[0].status == "SETTLED"


@pytest.mark.asyncio
async def test_skip_does_not_spend_last_run_capacity() -> None:
    """最後の残枠を認領して見送った CRON は、次の一件をまだ作れる。"""

    db = ScheduleDatabase(claimed=True)
    db.schedule.max_runs = 1
    await db.repo.record_outcome(db.claim, _outcome(db), now=NOW)
    assert db.schedule.status == "ACTIVE"
    assert db.schedule.run_count == 0
    assert (
        await db.repo.claim_due(
            _to_record(db.schedule), now=NOW + timedelta(minutes=1), worker_id="worker", token=TOKEN
        )
        is not None
    )


@pytest.mark.asyncio
async def test_late_summary_cannot_replace_later_occurrence_summary() -> None:
    """旧 occurrence の Run は数えるが、最新摘要の時刻を巻き戻さない。"""

    db = ScheduleDatabase(claimed=True)
    latest = uuid4()
    db.schedule.last_run_at = NOW + timedelta(minutes=5)
    db.schedule.last_run_id = latest
    db.schedule.last_outcome = "RUN_CREATED"
    await db.repo.record_outcome(db.claim, _outcome(db, run=True), now=NOW)
    assert db.schedule.run_count == 1
    assert db.schedule.last_run_id == latest
    assert db.schedule.last_run_at == NOW + timedelta(minutes=5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["QUEUED", "RUNNING", "WAITING_USER", "WAITING_APPROVAL", "SUCCEEDED", "FAILED", "CANCELLED"],
)
async def test_overlap_uses_all_associations_not_last_run_pointer(status: str) -> None:
    """古い非終態 Run と人工待機も重複に含み、終態だけを除外する。"""

    db = ScheduleDatabase(claimed=True)
    previous = occurrence_row(db.schedule)
    previous.id = uuid4()
    previous.status = "SETTLED"
    run = run_for_occurrence(previous, status=status)
    previous.run_id = run.id
    db.occurrences.append(previous)
    db.runs.append(run)
    db.schedule.last_run_id = uuid4()
    locked = await db.repo.lock_claim(db.claim, now=NOW)
    with patch("projectmind.schedules.repository_occurrences._now", return_value=NOW):
        assert await db.repo.has_overlapping_run(locked) is (
            status not in {"SUCCEEDED", "FAILED", "CANCELLED"}
        )
    assert "JOIN task_schedule_occurrences" in str(db.statements[-1])
    assert db.statements[-1]._for_update_arg is None


@pytest.mark.asyncio
async def test_original_request_validation_reuses_existing_intent_normalization() -> None:
    """別 actor・別入力・別 key は同じ occurrence の開始に使えない。"""

    db = ScheduleDatabase(claimed=True)
    claim = db.claim
    locked = await db.repo.lock_claim(claim, now=NOW)
    intent = TaskRunIntent(
        claim.project_id,
        claim.skill_version_id,
        claim.task_key,
        claim.created_by,
        claim.input_json,
        claim.sources,
    )
    db.repo.validate_original_request(
        locked, intent=intent, idempotency_key=db.occurrences[0].idempotency_key
    )
    with pytest.raises(ScheduleOccurrenceConflictError):
        db.repo.validate_original_request(
            locked,
            intent=replace(intent, actor_id=uuid4()),
            idempotency_key=db.occurrences[0].idempotency_key,
        )


@pytest.mark.asyncio
async def test_edit_capacity_includes_pending_and_status_is_cas_protected() -> None:
    """定義更新は予約中の一枠を無視せず、状態更新も読取後の競合を検出する。"""

    db = ScheduleDatabase(claimed=True)
    db.schedule.run_count = 1
    command = UpdateScheduleCommand(
        db.schedule.id,
        db.schedule.project_id,
        "New",
        replace(db.claim.definition, max_runs=1),
        {},
        {},
        NOW + timedelta(minutes=2),
        2,
    )
    with pytest.raises(ScheduleInvalidError):
        await db.repo.update_definition(command)
    with pytest.raises(ScheduleConflictError):
        await db.repo.set_status(
            project_id=db.schedule.project_id,
            schedule_id=db.schedule.id,
            status=ScheduleStatus.PAUSED,
            next_run_at=None,
            expected_row_version=1,
        )
    assert db.schedule.status == "ACTIVE"


@pytest.mark.asyncio
async def test_legacy_status_cannot_restore_automatic_firing() -> None:
    """通常の resume 操作を legacy 台帳の根拠のない移行として使わない。"""

    db = ScheduleDatabase()
    db.schedule.status = "ERROR"
    db.schedule.occurrence_protocol = 0
    with pytest.raises(ScheduleInvalidError):
        await db.repo.set_status(
            project_id=db.schedule.project_id,
            schedule_id=db.schedule.id,
            status=ScheduleStatus.ACTIVE,
            next_run_at=NOW,
            expected_row_version=1,
        )
    assert db.schedule.occurrence_protocol == 0


@pytest.mark.asyncio
async def test_claim_lease_begins_after_waiting_for_schedule_lock() -> None:
    """tick 開始時刻から長く待機しても期限切れの lease を発行しない。"""

    db = ScheduleDatabase()
    after_wait = NOW + timedelta(minutes=5)
    with patch("projectmind.schedules.repository_occurrences._now", return_value=after_wait):
        claim = await db.repo.claim_due(
            _to_record(db.schedule), now=NOW, worker_id="worker", token=TOKEN
        )
    assert claim is not None
    assert claim.lease_expires_at == after_wait + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_flush_failure_rolls_back_candidate_and_pending_insert() -> None:
    """A commit に達しない基盤失敗は、新しい候補を消費したことにしない。"""

    db = ScheduleDatabase()
    db.session.flush.side_effect = OSError("fixture unavailable")
    with pytest.raises(OSError):
        async with db.transaction():
            await db.repo.claim_due(
                _to_record(db.schedule), now=NOW, worker_id="worker", token=TOKEN
            )
    assert db.schedule.next_run_at == NOW
    assert db.schedule.row_version == 1
    assert not db.occurrences


@pytest.mark.asyncio
async def test_unknown_claim_commit_recovers_same_occurrence_not_new_candidate() -> None:
    """commit 済みだが応答不明という fake 状態では、期限後も原 ID/key を使う。"""

    db = ScheduleDatabase()
    first = await db.repo.claim_due(
        _to_record(db.schedule), now=NOW, worker_id="worker", token=TOKEN
    )
    assert first is not None
    original_key = db.occurrences[0].idempotency_key
    recovered = await db.repo.claim_recoverable(
        now=NOW + timedelta(seconds=60), limit=1, worker_id="recover", token="recovered"
    )
    assert recovered[0].occurrence_id == first.occurrence_id
    assert db.occurrences[0].idempotency_key == original_key
    assert db.schedule.run_count == 0
    assert len(db.occurrences) == 1


@pytest.mark.asyncio
async def test_completed_run_spends_last_capacity_and_prevents_resume() -> None:
    """実際に Run を関連付けた最後の枠だけが自動停止を確定する。"""

    db = ScheduleDatabase(claimed=True)
    db.schedule.max_runs = 1
    await db.repo.record_outcome(db.claim, _outcome(db, run=True), now=NOW)
    assert db.schedule.status == "COMPLETED"
    assert db.schedule.next_run_at is None
    assert db.schedule.run_count == 1
    db.schedule.status = "PAUSED"
    with pytest.raises(ScheduleInvalidError, match="exhausted"):
        await db.repo.set_status(
            project_id=db.schedule.project_id,
            schedule_id=db.schedule.id,
            status=ScheduleStatus.ACTIVE,
            next_run_at=NOW,
            expected_row_version=db.schedule.row_version,
        )


@pytest.mark.asyncio
async def test_candidate_not_matching_definition_never_creates_occurrence() -> None:
    """壊れた next_run_at を正しい cron の原予定であると補造しない。"""

    db = ScheduleDatabase()
    db.schedule.cron_expression = "30 * * * *"
    with pytest.raises(ScheduleOccurrenceConflictError):
        await db.repo.claim_due(_to_record(db.schedule), now=NOW, worker_id="worker", token=TOKEN)
    assert not db.occurrences


@pytest.mark.asyncio
async def test_definition_edit_preserves_pending_original_and_protocol() -> None:
    """新設定は次回以降に効き、元の snapshot や legacy マーカーを変更しない。"""

    db = ScheduleDatabase(claimed=True)
    original = deepcopy(db.occurrences[0].snapshot_json)
    command = UpdateScheduleCommand(
        db.schedule.id,
        db.schedule.project_id,
        "New",
        db.claim.definition,
        {"new": True},
        {},
        NOW + timedelta(minutes=3),
        2,
    )
    record = await db.repo.update_definition(command)
    assert record.configuration_version == 2
    assert record.input_json == {"new": True}
    assert db.occurrences[0].snapshot_json == original
    assert record.occurrence_protocol == 1


@pytest.mark.asyncio
async def test_status_transition_is_revalidated_after_lock() -> None:
    """同じ版でも禁止遷移を低層 repository から迂回しない。"""

    from projectmind.schedules.domain import InvalidScheduleTransitionError

    db = ScheduleDatabase()
    db.schedule.status = "ARCHIVED"
    with pytest.raises(InvalidScheduleTransitionError):
        await db.repo.set_status(
            project_id=db.schedule.project_id,
            schedule_id=db.schedule.id,
            status=ScheduleStatus.PAUSED,
            next_run_at=None,
            expected_row_version=1,
        )


@pytest.mark.asyncio
async def test_recovery_skips_locked_head_schedule_before_applying_limit() -> None:
    """先頭 A が lock 中でも LIMIT 1 の枠を消費せず、後ろの B を回復する。"""

    db = ScheduleDatabase()
    first = db.schedule
    first.id = UUID(int=1)
    second = schedule_row()
    second.id = UUID(int=2)
    db.schedules.append(second)
    first_occurrence = occurrence_row(first)
    second_occurrence = occurrence_row(second)
    db.occurrences.extend((first_occurrence, second_occurrence))
    db.locked_schedule_ids.add(first.id)
    result = await db.repo.claim_recoverable(
        now=NOW + timedelta(seconds=60),
        limit=1,
        worker_id="recovery",
        token="recovered",
    )
    assert [claim.schedule_id for claim in result] == [second.id]
    assert first_occurrence.lease_generation == 1
    assert first_occurrence.attempt_count == 1
    assert second_occurrence.lease_generation == 2
    assert second_occurrence.attempt_count == 2
    assert second_occurrence.status == "PENDING"
    dialect_factory: Callable[[], Dialect] = PGDialect
    candidate_sql = str(db.statements[0].compile(dialect=dialect_factory()))
    assert "JOIN task_schedule_occurrences" in candidate_sql
    assert "FOR UPDATE OF task_schedules SKIP LOCKED" in candidate_sql
    assert db.statements[0].compile().params["param_1"] == 1
    assert db.statements[0].get_execution_options()["populate_existing"] is True
    occurrence_sql = str(db.statements[1].compile(dialect=dialect_factory()))
    assert "FROM task_schedule_occurrences" in occurrence_sql
    assert "FOR UPDATE SKIP LOCKED" in occurrence_sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        ScheduleOutcome.SKIPPED_OVERLAP,
        ScheduleOutcome.FAILED_PRECONDITION,
    ],
)
async def test_nonrun_settlement_unknown_commit_can_confirm_original_after_expiry(
    outcome: ScheduleOutcome,
) -> None:
    """C commit 応答が不明でも、元の結算の確認は回数・版・状態を再更新しない。"""

    db = ScheduleDatabase(claimed=True)
    claim = db.claim
    original = replace(_outcome(db), outcome=outcome, detail="Fixture outcome")
    assert await db.repo.record_outcome(claim, original, now=NOW) == original
    version = db.schedule.row_version
    timestamp = db.schedule.updated_at
    status = db.schedule.status
    flushes = db.session.flush.await_count
    assert await db.repo.record_outcome(claim, original, now=NOW + timedelta(days=1)) == original
    assert db.schedule.row_version == version
    assert db.schedule.updated_at == timestamp
    assert db.schedule.status == status
    assert db.schedule.run_count == 0
    assert db.session.flush.await_count == flushes
    other = (
        ScheduleOutcome.FAILED_PRECONDITION
        if outcome is ScheduleOutcome.SKIPPED_OVERLAP
        else ScheduleOutcome.SKIPPED_OVERLAP
    )
    with pytest.raises(ScheduleOccurrenceConflictError):
        await db.repo.record_outcome(
            claim, replace(original, outcome=other), now=NOW + timedelta(days=1)
        )
    assert db.occurrences[0].outcome == outcome.value
    assert db.schedule.row_version == version
