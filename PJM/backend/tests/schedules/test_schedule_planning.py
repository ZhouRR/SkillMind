"""schedule 定義の検証と発火認領の決議を検証する (計画 §22 D1/D5/D6/D7/D9)。

ここで守るのは「保存できたのに必ず落ちる設定を作らない」「停止から復帰した瞬間に溜まった
回数だけ Run が並ばない」「同じ発火から二つ目の Run が生まれない」という三点。いずれも
本番でしか露見しにくく、露見したときの後始末が高い。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from projectmind.schedules import (
    ScheduleDefinition,
    ScheduleInvalidError,
    ScheduleKind,
    ScheduleStatus,
    build_definition,
    plan_occurrence,
    plan_schedule_transition,
    schedule_idempotency_key,
)
from projectmind.schedules.domain import InvalidScheduleTransitionError

_NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)


def _cron(
    expression: str = "0 * * * *",
    *,
    timezone: str = "UTC",
    end_at: datetime | None = None,
    max_runs: int | None = None,
) -> ScheduleDefinition:
    """テスト用の CRON 定義を組み立てる。"""

    return ScheduleDefinition(
        kind=ScheduleKind.CRON,
        timezone=timezone,
        cron_expression=expression,
        run_at=None,
        end_at=end_at,
        max_runs=max_runs,
    )


def test_cron_definition_rejects_a_fixed_instant() -> None:
    """CRON に単発時刻を混ぜた定義を拒否する (発火時刻が一意に決まらない)。"""

    with pytest.raises(ScheduleInvalidError):
        build_definition(
            kind="CRON",
            timezone="UTC",
            cron_expression="0 * * * *",
            run_at=_NOW + timedelta(days=1),
            end_at=None,
            max_runs=None,
            now=_NOW,
        )


def test_one_shot_definition_requires_a_future_instant() -> None:
    """過去の単発予約は保存前に拒否する。"""

    with pytest.raises(ScheduleInvalidError):
        build_definition(
            kind="ONCE",
            timezone="UTC",
            cron_expression=None,
            run_at=_NOW - timedelta(hours=2),
            end_at=None,
            max_runs=None,
            now=_NOW,
        )


def test_one_shot_definition_truncates_to_the_minute() -> None:
    """単発予約は分単位へ正規化される (cron の粒度と揃える)。"""

    definition = build_definition(
        kind="ONCE",
        timezone="Asia/Tokyo",
        cron_expression=None,
        run_at=datetime(2026, 8, 1, 3, 4, 56, 789, tzinfo=UTC),
        end_at=None,
        max_runs=None,
        now=_NOW,
    )

    assert definition.run_at == datetime(2026, 8, 1, 3, 4, tzinfo=UTC)


def test_definition_rejects_an_end_that_already_passed() -> None:
    """既に過ぎた終了時刻を持つ定義は保存させない (作った瞬間に死んでいる)。"""

    with pytest.raises(ScheduleInvalidError):
        build_definition(
            kind="CRON",
            timezone="UTC",
            cron_expression="0 * * * *",
            run_at=None,
            end_at=_NOW - timedelta(minutes=1),
            max_runs=None,
            now=_NOW,
        )


@pytest.mark.parametrize("field", ["run_at", "end_at", "now"])
def test_definition_rejects_naive_timestamps(field: str) -> None:
    """時区なし入力は API を経由しない use case でも拒否する。"""

    moments = {"run_at": _NOW + timedelta(days=1), "end_at": _NOW + timedelta(days=2), "now": _NOW}
    moments[field] = moments[field].replace(tzinfo=None)
    with pytest.raises(ScheduleInvalidError, match="UTC offset"):
        build_definition(
            kind="ONCE",
            timezone="Asia/Tokyo",
            cron_expression=None,
            run_at=moments["run_at"],
            end_at=moments["end_at"],
            max_runs=None,
            now=moments["now"],
        )


def test_missed_count_stops_at_the_original_end() -> None:
    """終了時刻より後の候補を見送り監査へ補造しない。"""

    plan = plan_occurrence(
        _cron(end_at=_NOW + timedelta(hours=2)),
        occurrence=_NOW,
        now=_NOW + timedelta(hours=6),
    )
    assert plan.missed == 2
    assert plan.exhausted is True


def test_missed_occurrences_are_counted_but_not_replayed() -> None:
    """停止中に過ぎた発火は回数だけ数え、次回は「今より後」に取る (§22 D6)。

    追いかけると復帰の瞬間に溜まった回数ぶん Run が並び、外部 system へ突発的な負荷になる。
    """

    # 毎時 0 分の schedule が 03:00 で止まり、09:00 過ぎに復帰した状況。
    plan = plan_occurrence(
        _cron("0 * * * *"),
        occurrence=datetime(2026, 7, 26, 3, 0, tzinfo=UTC),
        now=datetime(2026, 7, 26, 9, 30, tzinfo=UTC),
        run_count=0,
    )

    # 04:00〜09:00 の 6 回は見送り、次回は 10:00。
    assert plan.missed == 6
    assert plan.next_run_at == datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    assert plan.exhausted is False


def test_on_time_trigger_reports_no_missed_occurrence() -> None:
    """遅れなく発火したときは見送り 0 件になる。"""

    plan = plan_occurrence(
        _cron("0 * * * *"),
        occurrence=datetime(2026, 7, 26, 9, 0, tzinfo=UTC),
        now=datetime(2026, 7, 26, 9, 0, 12, tzinfo=UTC),
        run_count=3,
    )

    assert plan.missed == 0
    assert plan.next_run_at == datetime(2026, 7, 26, 10, 0, tzinfo=UTC)


def test_one_shot_schedule_is_exhausted_after_its_only_trigger() -> None:
    """単発予約は一度発火したら打ち切る。"""

    definition = ScheduleDefinition(
        kind=ScheduleKind.ONCE,
        timezone="UTC",
        cron_expression=None,
        run_at=_NOW,
        end_at=None,
        max_runs=None,
    )

    plan = plan_occurrence(definition, occurrence=_NOW, now=_NOW, run_count=0)

    assert plan.exhausted is True
    assert plan.missed == 0


def test_reserved_final_slot_does_not_exhaust_the_time_plan() -> None:
    """認領は作成ではないため、最後の名額で重複見送りしても次回を残す。"""

    plan = plan_occurrence(
        _cron(max_runs=3),
        occurrence=_NOW,
        now=_NOW,
        run_count=2,
    )

    assert plan.exhausted is False
    assert plan.next_run_at == _NOW + timedelta(hours=1)


def test_run_limit_keeps_the_schedule_alive_before_the_final_trigger() -> None:
    """上限手前では打ち切らない。"""

    plan = plan_occurrence(
        _cron(max_runs=3),
        occurrence=_NOW,
        now=_NOW,
        run_count=1,
    )

    assert plan.exhausted is False


def test_end_at_exhausts_the_schedule_when_the_next_one_is_beyond_it() -> None:
    """次回が終了時刻を越えるなら打ち切る。"""

    plan = plan_occurrence(
        _cron(end_at=datetime(2026, 7, 26, 9, 30, tzinfo=UTC)),
        occurrence=_NOW,
        now=_NOW,
        run_count=0,
    )

    assert plan.exhausted is True


def test_idempotency_key_is_stable_and_timezone_independent() -> None:
    """同じ発火からは常に同じ key が出る (§22 D7)。

    host の local timezone に依存すると、worker の TZ 設定が違うだけで二重発火の防止が崩れる。
    """

    schedule_id = UUID("11111111-2222-3333-4444-555555555555")
    from zoneinfo import ZoneInfo

    utc_form = schedule_idempotency_key(
        schedule_id=schedule_id, occurrence_at=datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    )
    tokyo_form = schedule_idempotency_key(
        schedule_id=schedule_id,
        occurrence_at=datetime(2026, 7, 26, 18, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
    )

    assert utc_form == tokyo_form
    assert utc_form == f"schedule:{schedule_id}:2026-07-26T09:00:00Z"


def test_different_occurrences_get_different_keys() -> None:
    """別の発火は別の key になる (二回目が replay に潰されない)。"""

    schedule_id = UUID("11111111-2222-3333-4444-555555555555")

    first = schedule_idempotency_key(
        schedule_id=schedule_id, occurrence_at=datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    )
    second = schedule_idempotency_key(
        schedule_id=schedule_id, occurrence_at=datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    )

    assert first != second


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ScheduleStatus.ACTIVE, ScheduleStatus.PAUSED),
        (ScheduleStatus.PAUSED, ScheduleStatus.ACTIVE),
        (ScheduleStatus.ERROR, ScheduleStatus.ACTIVE),
        (ScheduleStatus.COMPLETED, ScheduleStatus.ARCHIVED),
    ],
)
def test_allowed_status_transitions(current: ScheduleStatus, target: ScheduleStatus) -> None:
    """設定を直した後の手動復帰を含む正当な遷移を許す。"""

    assert plan_schedule_transition(current=current, target=target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ScheduleStatus.ARCHIVED, ScheduleStatus.ACTIVE),
        (ScheduleStatus.COMPLETED, ScheduleStatus.ACTIVE),
        (ScheduleStatus.ERROR, ScheduleStatus.PAUSED),
    ],
)
def test_rejected_status_transitions(current: ScheduleStatus, target: ScheduleStatus) -> None:
    """終態からの復活と、ERROR の素通りを拒否する。

    ERROR は「凍結した版や資源が失効した」状態なので、設定を直したという明示操作 (ACTIVE) 以外で
    抜けさせない。自動復帰させると D4 の「黙って別の来源へ切り替えない」が骨抜きになる。
    """

    with pytest.raises(InvalidScheduleTransitionError):
        plan_schedule_transition(current=current, target=target)
