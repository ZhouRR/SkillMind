"""Schedule の時間規則を保存・preview・認領で共有する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from projectmind.schedules.cron import (
    CronExpressionError,
    next_occurrence,
    normalize_timezone,
    parse_cron,
    upcoming_occurrences,
)
from projectmind.schedules.domain import (
    MAX_SCHEDULE_NAME_LENGTH,
    ScheduleDefinition,
    ScheduleInvalidError,
    ScheduleKind,
    ScheduleRecord,
)

PREVIEW_OCCURRENCE_COUNT = 5
# 保存直前の時計差だけを許容し、過去の任意補走は許可しない。
_PAST_TOLERANCE = timedelta(minutes=1)


def _aware(value: datetime) -> datetime:
    """プロセスの local timezone に依存する暗黙変換を拒否する。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleInvalidError("Schedule timestamps must include a UTC offset")
    return value.astimezone(UTC)


def build_definition(
    *,
    kind: str,
    timezone: str,
    cron_expression: str | None,
    run_at: datetime | None,
    end_at: datetime | None,
    max_runs: int | None,
    now: datetime | None = None,
) -> ScheduleDefinition:
    """入力から検証済みの `ScheduleDefinition` を作る。API と service が同じ検証を通す。"""

    reference = _aware(now or datetime.now(UTC))
    if run_at is not None:
        run_at = _aware(run_at)
    if end_at is not None:
        end_at = _aware(end_at)
    try:
        schedule_kind = ScheduleKind(kind)
    except ValueError as error:
        raise ScheduleInvalidError("Schedule kind is not supported") from error
    try:
        zone = normalize_timezone(timezone)
    except CronExpressionError as error:
        raise ScheduleInvalidError(str(error)) from error
    if schedule_kind is ScheduleKind.CRON:
        if not cron_expression:
            raise ScheduleInvalidError("Cron schedule requires an expression")
        if run_at is not None:
            raise ScheduleInvalidError("Cron schedule must not fix a single instant")
        try:
            parse_cron(cron_expression)
        except CronExpressionError as error:
            raise ScheduleInvalidError(str(error)) from error
        normalized_cron: str | None = " ".join(cron_expression.split())
        normalized_run_at: datetime | None = None
    else:
        if run_at is None:
            raise ScheduleInvalidError("One-shot schedule requires an instant")
        if cron_expression:
            raise ScheduleInvalidError("One-shot schedule must not carry an expression")
        normalized_run_at = run_at.astimezone(UTC).replace(second=0, microsecond=0)
        if normalized_run_at < reference - _PAST_TOLERANCE:
            raise ScheduleInvalidError("One-shot schedule is in the past")
        normalized_cron = None
    if max_runs is not None and (type(max_runs) is not int or not 1 <= max_runs <= 100_000):
        raise ScheduleInvalidError("Schedule run limit must be positive")
    normalized_end = end_at.astimezone(UTC) if end_at is not None else None
    if normalized_end is not None and normalized_end <= reference:
        raise ScheduleInvalidError("Schedule end is already in the past")
    return ScheduleDefinition(
        kind=schedule_kind,
        timezone=zone,
        cron_expression=normalized_cron,
        run_at=normalized_run_at,
        end_at=normalized_end,
        max_runs=max_runs,
    )


@dataclass(frozen=True, slots=True)
class OccurrencePlan:
    """一回の発火を認領するときに決まること。"""

    next_run_at: datetime | None
    missed: int
    exhausted: bool


def plan_occurrence(
    definition: ScheduleDefinition,
    *,
    occurrence: datetime,
    now: datetime,
    run_count: int = 0,
) -> OccurrencePlan:
    """認領時の「次回候補・見送り回数・打ち切りか」を決める純関数 (§22 D6/D9)。

    停止中に過ぎた分は数えるだけで走らせない。次回候補を「今より後の最初の一致」に取ることで、
    復帰の瞬間に溜まった回数だけ Run が並ぶ状態を構造的に避ける。
    """

    following = next_from_definition(definition, after=now)
    missed = _count_missed(definition, start=occurrence, until=now)
    exhausted = (
        definition.kind is ScheduleKind.ONCE
        or following is None
        or (definition.end_at is not None and following > definition.end_at)
    )
    return OccurrencePlan(next_run_at=following, missed=missed, exhausted=exhausted)


def occurrences(definition: ScheduleDefinition, *, after: datetime, count: int) -> list[datetime]:
    """定義が生む発火時刻を最大 `count` 件、`end_at` で打ち切って返す。"""

    if definition.kind is ScheduleKind.ONCE:
        instant = definition.run_at
        if instant is None or instant <= after:
            return []
        if definition.end_at is not None and instant > definition.end_at:
            return []
        return [instant]
    expression = parse_cron(definition.cron_expression or "")
    limit = count
    if definition.max_runs is not None:
        limit = min(limit, definition.max_runs)
    candidates = upcoming_occurrences(
        expression, after=after, timezone=definition.timezone, count=limit
    )
    if definition.end_at is not None:
        candidates = [item for item in candidates if item <= definition.end_at]
    return candidates


def first_occurrence(definition: ScheduleDefinition) -> datetime:
    """保存に必要な最初の発火時刻。存在しなければ保存させない。"""

    candidates = occurrences(definition, after=datetime.now(UTC), count=1)
    if not candidates:
        raise ScheduleInvalidError("Schedule has no future occurrence")
    return candidates[0]


def next_from_definition(definition: ScheduleDefinition, *, after: datetime) -> datetime | None:
    """次の発火時刻。尽きていれば None。"""

    candidates = occurrences(definition, after=after, count=1)
    return candidates[0] if candidates else None


def _count_missed(definition: ScheduleDefinition, *, start: datetime, until: datetime) -> int:
    """`start` の次から `until` までに過ぎた発火回数を数える (§22 D6 の記録用)。

    追いかけないと決めた回数そのものなので、監査のために残す。数え上げも有界にする。
    """

    if definition.kind is ScheduleKind.ONCE:
        return 0
    expression = parse_cron(definition.cron_expression or "")
    missed = 0
    cursor = start
    while missed < 1000:
        upcoming = next_occurrence(expression, after=cursor, timezone=definition.timezone)
        if (
            upcoming is None
            or upcoming > until
            or (definition.end_at is not None and upcoming > definition.end_at)
        ):
            return missed
        missed += 1
        cursor = upcoming
    return missed


def definition_of(record: ScheduleRecord) -> ScheduleDefinition:
    """保存済み行から発火定義を復元する。"""

    return ScheduleDefinition(
        kind=record.kind,
        timezone=record.timezone,
        cron_expression=record.cron_expression,
        run_at=record.run_at,
        end_at=record.end_at,
        max_runs=record.max_runs,
    )


def validate_name(name: str) -> str:
    """一覧に出す短い名前だけを受け付ける。"""

    cleaned = name.strip()
    if not cleaned or len(cleaned) > MAX_SCHEDULE_NAME_LENGTH:
        raise ScheduleInvalidError("Schedule name is invalid")
    return cleaned
