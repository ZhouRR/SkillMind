"""Schedule → occurrence の短い transaction で認領・回復・一回だけの結算を行う。"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, func, select
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.core.hashing import canonical_json
from skillmind.db.models import Run, TaskSchedule, TaskScheduleOccurrence, User
from skillmind.runs.creation_request import TaskRunIntent
from skillmind.runs.domain import TERMINAL_RUN_STATUSES, lease_token_hash
from skillmind.runs.repository import RunRepository
from skillmind.schedules.domain import (
    DEFAULT_SCHEDULE_MAX_ATTEMPTS,
    ClaimedSchedule,
    ScheduleActivity,
    ScheduleActivityUnavailableError,
    ScheduleClaimLostError,
    ScheduleConflictError,
    ScheduleDefinition,
    ScheduleKind,
    ScheduleNotFoundError,
    ScheduleOccurrenceConflictError,
    ScheduleOutcome,
    SchedulePendingOccurrence,
    ScheduleRecord,
    ScheduleTracking,
    ScheduleTriggerResult,
)
from skillmind.schedules.occurrence import (
    ScheduleOccurrenceSnapshot,
    definition_json,
    positive_integer,
    utc_time,
)
from skillmind.schedules.planning import next_from_definition, plan_occurrence
from skillmind.users.repository import lock_organization


@dataclass(frozen=True, slots=True)
class LockedScheduleOccurrence:
    """同じ session でだけ利用する、Schedule が先に lock された原認領。"""

    schedule: TaskSchedule
    occurrence: TaskScheduleOccurrence
    claim: ClaimedSchedule


def _now(value: datetime | None = None) -> datetime:
    """test の明示時計以外では各 await 後に実時刻を読み直す。"""

    return utc_time(value if value is not None else datetime.now(UTC))


def _credentials(worker_id: str, token: str, lease_seconds: int) -> None:
    """空の owner/token と無限の lease を永続化させない。"""

    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
        raise ValueError("Schedule worker identity is invalid")
    if not isinstance(token, str) or not token.strip() or len(token) > 512:
        raise ValueError("Schedule lease token is invalid")
    positive_integer(lease_seconds, name="lease_seconds", maximum=3600)


def _bump(row: TaskSchedule, *, now: datetime) -> None:
    """世代の wrap を許さず、既存の row CAS を維持する。"""

    positive_integer(row.row_version, name="row_version")
    if row.row_version == 2_147_483_647:
        raise ScheduleConflictError("Schedule version is exhausted")
    row.row_version += 1
    row.updated_at = now


class ScheduleOccurrenceRepository:
    """普通 Run 作成に参加する永続認領の repository 部分を提供する。"""

    def __init__(self, session: AsyncSession) -> None:
        """呼出し元が所有する transaction を共有する。"""

        self._session = session

    async def get_activity(self, *, project_id: UUID, schedule_id: UUID) -> ScheduleActivity:
        """同一 SELECT の親・PENDING・DB 時刻を読み、業務 lock や書込みを発生させない。"""

        result = await self._session.execute(
            select(
                TaskSchedule,
                TaskScheduleOccurrence,
                func.statement_timestamp(type_=DateTime(timezone=True)).label("checked_at"),
            )
            .outerjoin(
                TaskScheduleOccurrence,
                (TaskScheduleOccurrence.schedule_id == TaskSchedule.id)
                & (TaskScheduleOccurrence.status == "PENDING"),
            )
            .where(TaskSchedule.id == schedule_id, TaskSchedule.project_id == project_id)
            .execution_options(populate_existing=True, autoflush=False)
        )
        try:
            # LIMIT/first は破損した複数 PENDING を隠すため使わない。
            observed = result.one_or_none()
        except MultipleResultsFound as error:
            raise ScheduleActivityUnavailableError("Schedule activity is unavailable") from error
        if observed is None:
            raise ScheduleNotFoundError("Schedule was not found")
        schedule, pending, checked_at = observed
        try:
            return _activity(schedule, pending, checked_at=checked_at)
        except (ScheduleOccurrenceConflictError, ValueError, TypeError, AttributeError) as error:
            raise ScheduleActivityUnavailableError("Schedule activity is unavailable") from error

    async def _require_row(
        self, *, project_id: UUID, schedule_id: UUID, lock: bool = False
    ) -> TaskSchedule:
        """lock 待機前の identity map を現在の DB 行で置き換える。"""

        statement = select(TaskSchedule).where(
            TaskSchedule.id == schedule_id, TaskSchedule.project_id == project_id
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = await self._session.scalar(statement)
        if row is None:
            raise ScheduleNotFoundError("Schedule was not found")
        return row

    async def _pending_count(self, schedule_id: UUID) -> int:
        """同 Schedule mutex の内側で未決一件を残枠に含める。"""

        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(TaskScheduleOccurrence)
                .where(
                    TaskScheduleOccurrence.schedule_id == schedule_id,
                    TaskScheduleOccurrence.status == "PENDING",
                )
            )
            or 0
        )

    async def claim_due(
        self,
        record: ScheduleRecord,
        *,
        now: datetime,
        worker_id: str,
        token: str,
        lease_seconds: int = 60,
    ) -> ClaimedSchedule | None:
        """元行の CAS と原設定の保存、次候補の更新を一回の commit に閉じ込める。"""

        _credentials(worker_id, token, lease_seconds)
        reference = utc_time(now)
        row = await self._require_row(
            project_id=record.project_id, schedule_id=record.schedule_id, lock=True
        )
        if (
            row.status != "ACTIVE"
            or row.row_version != record.row_version
            or row.configuration_version != record.configuration_version
            or row.next_run_at != record.next_run_at
            or row.next_run_at is None
            or row.next_run_at > reference
        ):
            return None
        if row.occurrence_protocol != 1:
            # 旧摘要の回数・Run 関連は正確な残枠の証明ではない。消去も補造もしない。
            row.status = "ERROR"
            row.next_run_at = None
            row.last_error = "Legacy schedule requires explicit migration before activation"
            _bump(row, now=reference)
            await self._session.flush()
            return None
        pending = await self._pending_count(row.id)
        if pending:
            return None
        if row.max_runs is not None and row.run_count >= row.max_runs:
            row.status = "COMPLETED"
            row.next_run_at = None
            _bump(row, now=reference)
            await self._session.flush()
            return None
        occurrence = utc_time(row.next_run_at, minute=True)
        definition = ScheduleDefinition(
            ScheduleKind(row.kind),
            row.timezone,
            row.cron_expression,
            row.run_at,
            row.end_at,
            row.max_runs,
        )
        if next_from_definition(definition, after=occurrence - timedelta(minutes=1)) != occurrence:
            raise ScheduleOccurrenceConflictError(
                "Schedule candidate does not match its definition"
            )
        plan = plan_occurrence(definition, occurrence=occurrence, now=reference)
        snapshot = ScheduleOccurrenceSnapshot(
            schedule_id=row.id,
            occurrence_at=occurrence,
            name=row.name,
            configuration_version=row.configuration_version,
            claim_row_version=row.row_version + 1,
            definition=definition,
            intent=TaskRunIntent(
                project_id=row.project_id,
                skill_version_id=row.skill_version_id,
                task_key=row.task_key,
                actor_id=row.created_by,
                input_json=row.input_json,
                sources=row.sources_json,
            ),
            exhausted=plan.exhausted,
            missed=plan.missed,
        )
        snapshot = ScheduleOccurrenceSnapshot.from_json(snapshot.to_json())
        lease_started_at = max(reference, _now())
        occurrence_row = TaskScheduleOccurrence(
            id=uuid4(),
            schedule_id=row.id,
            project_id=row.project_id,
            created_by=row.created_by,
            skill_version_id=row.skill_version_id,
            occurrence_at=occurrence,
            idempotency_key=snapshot.idempotency_key,
            configuration_version=row.configuration_version,
            snapshot_json=snapshot.to_json(),
            snapshot_checksum=snapshot.checksum,
            status="PENDING",
            worker_id=worker_id,
            lease_token_hash=lease_token_hash(token),
            lease_generation=1,
            lease_expires_at=lease_started_at + timedelta(seconds=lease_seconds),
            attempt_count=1,
            run_id=None,
            outcome=None,
            detail=None,
            settled_at=None,
            created_at=reference,
            updated_at=reference,
        )
        self._session.add(occurrence_row)
        row.next_run_at = plan.next_run_at
        row.missed_count += plan.missed
        _bump(row, now=reference)
        await self._session.flush()
        claim = _claimed(occurrence_row, token=token)
        _require_fence(occurrence_row, claim, now=_now())
        return claim

    async def claim_recoverable(
        self,
        *,
        now: datetime,
        limit: int,
        worker_id: str,
        token: str,
        lease_seconds: int = 60,
        max_attempts: int = DEFAULT_SCHEDULE_MAX_ATTEMPTS,
    ) -> list[ClaimedSchedule]:
        """期限切れの同じ原 occurrence を新世代で引き継ぎ、上限後も未決を残す。"""

        _credentials(worker_id, token, lease_seconds)
        positive_integer(limit, name="limit", maximum=1000)
        positive_integer(max_attempts, name="max_attempts")
        reference = utc_time(now)
        candidates = list(
            await self._session.scalars(
                select(TaskSchedule)
                .join(TaskScheduleOccurrence, TaskScheduleOccurrence.schedule_id == TaskSchedule.id)
                .where(
                    TaskSchedule.occurrence_protocol == 1,
                    TaskScheduleOccurrence.status == "PENDING",
                    TaskScheduleOccurrence.lease_expires_at <= reference,
                    TaskScheduleOccurrence.attempt_count < max_attempts,
                )
                .order_by(TaskScheduleOccurrence.schedule_id, TaskScheduleOccurrence.occurrence_at)
                .limit(limit)
                # LIMIT 前に親の lock を取得して、先頭の競合で後続を飢餓にしない。
                # OF を省くと join 先も先に lock し、Schedule → occurrence 順が崩れる。
                .with_for_update(of=TaskSchedule, skip_locked=True)
                .execution_options(populate_existing=True)
            )
        )
        result: list[ClaimedSchedule] = []
        for schedule in candidates:
            # 親だけを先に固定済み。元の PENDING は世代と期限を別の lock 下で再確認する。
            row = await self._session.scalar(
                select(TaskScheduleOccurrence)
                .where(
                    TaskScheduleOccurrence.schedule_id == schedule.id,
                    TaskScheduleOccurrence.status == "PENDING",
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if (
                row is None
                or row.status != "PENDING"
                or row.lease_expires_at > reference
                or row.attempt_count >= max_attempts
            ):
                continue
            occurrence_snapshot(row)
            if row.schedule_id != schedule.id or row.project_id != schedule.project_id:
                raise ScheduleOccurrenceConflictError("Occurrence scope does not match")
            positive_integer(row.lease_generation + 1, name="lease_generation")
            row.worker_id = worker_id
            row.lease_token_hash = lease_token_hash(token)
            row.lease_generation += 1
            row.attempt_count += 1
            row.lease_expires_at = max(reference, _now()) + timedelta(seconds=lease_seconds)
            row.updated_at = reference
            result.append(_claimed(row, token=token))
        await self._session.flush()
        for claim in result:
            if claim.lease_expires_at <= _now():
                raise ScheduleClaimLostError("Recovered schedule claim expired before commit")
        return result

    async def lock_creator(self, claim: ClaimedSchedule) -> User | None:
        """管理 writer の組織 gate 後に現在の User を SHARE で固定する。"""

        organization_id = await self._session.scalar(
            select(User.organization_id).where(User.id == claim.created_by)
        )
        if organization_id is None:
            return None
        await lock_organization(self._session, organization_id)
        user = await self._session.scalar(
            select(User)
            .where(User.id == claim.created_by)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        return user if user is not None and user.organization_id == organization_id else None

    async def lock_claim(
        self,
        claim: ClaimedSchedule,
        *,
        now: datetime | None = None,
    ) -> LockedScheduleOccurrence:
        """取得済みの外側権限 lock に続き、元の要求と認領世代を再検証する。"""

        schedule = await self._require_row(
            project_id=claim.project_id, schedule_id=claim.schedule_id, lock=True
        )
        row = await self._session.scalar(
            select(TaskScheduleOccurrence)
            .where(
                TaskScheduleOccurrence.id == claim.occurrence_id,
                TaskScheduleOccurrence.schedule_id == claim.schedule_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or schedule.occurrence_protocol != 1:
            raise ScheduleClaimLostError("Schedule claim is unavailable")
        snapshot = occurrence_snapshot(row)
        _match_claim(row, snapshot, claim)
        _require_fence(row, claim, now=_now(now))
        return LockedScheduleOccurrence(schedule, row, claim)

    @staticmethod
    def validate_original_request(
        locked: LockedScheduleOccurrence,
        *,
        intent: TaskRunIntent,
        idempotency_key: str,
    ) -> None:
        """元の Run 要求規則を再利用し、更新後の入力や別の key に置換させない。"""

        snapshot = occurrence_snapshot(locked.occurrence)
        if snapshot.idempotency_key != idempotency_key or canonical_json(
            snapshot.intent.to_json()
        ) != canonical_json(intent.to_json()):
            raise ScheduleOccurrenceConflictError(
                "Scheduled creation does not match original request"
            )

    async def has_overlapping_run(self, locked: LockedScheduleOccurrence) -> bool:
        """最終摘要でなく全関連 Run を調べ、待機状態も重複として扱う。"""

        _require_fence(locked.occurrence, locked.claim, now=_now())
        run_id = await self._session.scalar(
            select(Run.id)
            .join(TaskScheduleOccurrence, TaskScheduleOccurrence.run_id == Run.id)
            .where(
                TaskScheduleOccurrence.schedule_id == locked.schedule.id,
                TaskScheduleOccurrence.id != locked.occurrence.id,
                Run.status.not_in(tuple(item.value for item in TERMINAL_RUN_STATUSES)),
            )
            .limit(1)
        )
        _require_fence(locked.occurrence, locked.claim, now=_now())
        return run_id is not None

    async def record_outcome(
        self,
        claim: ClaimedSchedule,
        result: ScheduleTriggerResult,
        *,
        now: datetime | None = None,
    ) -> ScheduleTriggerResult:
        """Run 関連と回数を同 transaction で一回だけ確定し、再確認で再加算しない。"""

        locked = await self.lock_claim(claim, now=now)
        row, schedule = locked.occurrence, locked.schedule
        _validate_result(row, result)
        if row.status == "SETTLED":
            existing = _result(row)
            if existing != result:
                raise ScheduleOccurrenceConflictError("Occurrence was settled differently")
            return existing
        if result.run_id is not None:
            snapshot = occurrence_snapshot(row)
            existing_run = await RunRepository(self._session).find_task_run_replay(
                intent=snapshot.intent, idempotency_key=snapshot.idempotency_key
            )
            if existing_run is None or existing_run.run_id != result.run_id:
                raise ScheduleOccurrenceConflictError("Run does not match original occurrence")
        reference = _now(now)
        _require_fence(row, claim, now=reference)
        unchanged = (
            schedule.configuration_version == claim.configuration_version
            and schedule.row_version == claim.claim_row_version
        )
        row.status = "SETTLED"
        row.outcome = result.outcome.value
        row.run_id = result.run_id
        row.detail = result.detail
        row.settled_at = reference
        row.updated_at = reference
        if result.run_id is not None:
            schedule.run_count += 1
        # 遅れて完了した旧世代から、新しい表示摘要を巻き戻さない。
        if schedule.last_run_at is None or row.occurrence_at > schedule.last_run_at:
            schedule.last_run_at = row.occurrence_at
            schedule.last_outcome = result.outcome.value
            schedule.last_error = result.detail
            if result.run_id is not None:
                schedule.last_run_id = result.run_id
        if unchanged and schedule.status == "ACTIVE":
            if result.outcome is ScheduleOutcome.FAILED_PRECONDITION:
                schedule.status = "ERROR"
            elif claim.exhausted or (
                schedule.max_runs is not None and schedule.run_count >= schedule.max_runs
            ):
                schedule.status = "COMPLETED"
            if schedule.status != "ACTIVE":
                schedule.next_run_at = None
        _bump(schedule, now=reference)
        await self._session.flush()
        # 自分の SETTLED 化で期限検証を省略しない。commit 直前まで元の lease が必要。
        _require_fence(row, claim, now=_now(now), pending_write=True)
        return result


def _activity(
    schedule: TaskSchedule,
    pending: TaskScheduleOccurrence | None,
    *,
    checked_at: datetime,
) -> ScheduleActivity:
    """原台帳の整合性を確かめ、期限を過ぎた未決も消さずに白名単へ投影する。"""

    if type(schedule.occurrence_protocol) is not int or schedule.occurrence_protocol not in (0, 1):
        raise ValueError("Unsupported occurrence protocol")
    row_version = positive_integer(schedule.row_version, name="row_version")
    configuration_version = positive_integer(
        schedule.configuration_version, name="configuration_version"
    )
    projection = None
    if pending is not None:
        snapshot = occurrence_snapshot(pending)
        # project_id を JOIN 条件に置くと壊れた関連を null に見せるため、読取後に拒否する。
        # 編集で変わる name/definition は比較せず、原 identity と過去の版だけを検査する。
        if (
            schedule.occurrence_protocol != 1
            or pending.schedule_id != schedule.id
            or pending.project_id != schedule.project_id
            or pending.created_by != schedule.created_by
            or pending.skill_version_id != schedule.skill_version_id
            or snapshot.intent.task_key != schedule.task_key
            or pending.configuration_version > configuration_version
            or snapshot.claim_row_version > row_version
            or pending.status != "PENDING"
            or pending.run_id is not None
            or pending.outcome is not None
            or pending.settled_at is not None
            or not isinstance(pending.id, UUID)
        ):
            raise ValueError("Stored pending scope or state is invalid")
        positive_integer(pending.lease_generation, name="lease_generation")
        projection = SchedulePendingOccurrence(
            occurrence_id=pending.id,
            occurrence_at=utc_time(pending.occurrence_at, minute=True),
            configuration_version=pending.configuration_version,
            created_at=utc_time(pending.created_at),
            updated_at=utc_time(pending.updated_at),
            attempt_count=positive_integer(pending.attempt_count, name="attempt_count"),
            lease_expires_at=utc_time(pending.lease_expires_at),
        )
    return ScheduleActivity(
        schedule_id=schedule.id,
        project_id=schedule.project_id,
        row_version=row_version,
        configuration_version=configuration_version,
        tracking=(
            ScheduleTracking.TRACKED
            if schedule.occurrence_protocol == 1
            else ScheduleTracking.LEGACY_UNAVAILABLE
        ),
        checked_at=utc_time(checked_at),
        automatic_attempt_limit=DEFAULT_SCHEDULE_MAX_ATTEMPTS,
        pending=projection,
    )


def occurrence_snapshot(row: TaskScheduleOccurrence) -> ScheduleOccurrenceSnapshot:
    """JSON の実値 checksum と索引列の双方を照合する。"""

    snapshot = ScheduleOccurrenceSnapshot.from_json(
        row.snapshot_json, checksum=row.snapshot_checksum
    )
    if (
        row.schedule_id != snapshot.schedule_id
        or row.project_id != snapshot.intent.project_id
        or row.created_by != snapshot.intent.actor_id
        or row.skill_version_id != snapshot.intent.skill_version_id
        or row.occurrence_at != snapshot.occurrence_at
        or row.idempotency_key != snapshot.idempotency_key
        or row.configuration_version != snapshot.configuration_version
    ):
        raise ScheduleOccurrenceConflictError("Occurrence columns do not match original snapshot")
    return snapshot


def _claimed(row: TaskScheduleOccurrence, *, token: str) -> ClaimedSchedule:
    """回復後も原設定を使い、現 Schedule の変更を混ぜない。"""

    snapshot = occurrence_snapshot(row)
    return ClaimedSchedule(
        schedule_id=row.schedule_id,
        project_id=row.project_id,
        occurrence_at=row.occurrence_at,
        skill_version_id=row.skill_version_id,
        task_key=snapshot.intent.task_key,
        input_json=snapshot.intent.to_json()["input"],
        sources=dict(snapshot.intent.sources),
        created_by=row.created_by,
        exhausted=snapshot.exhausted,
        missed=snapshot.missed,
        occurrence_id=row.id,
        definition=snapshot.definition,
        configuration_version=row.configuration_version,
        claim_row_version=snapshot.claim_row_version,
        snapshot_checksum=row.snapshot_checksum,
        worker_id=row.worker_id,
        lease_token=token,
        lease_generation=row.lease_generation,
        lease_expires_at=row.lease_expires_at,
    )


def _match_claim(
    row: TaskScheduleOccurrence,
    snapshot: ScheduleOccurrenceSnapshot,
    claim: ClaimedSchedule,
) -> None:
    """DTO は認可ではないため、可変 dict を含め全原要求を保存値と照合する。"""

    original: dict[str, Any] = {
        "request": snapshot.intent.to_json(),
        "definition": definition_json(snapshot.definition),
        "exhausted": snapshot.exhausted,
        "missed": snapshot.missed,
        "configuration_version": snapshot.configuration_version,
        "claim_row_version": snapshot.claim_row_version,
    }
    candidate = {
        "request": TaskRunIntent(
            project_id=claim.project_id,
            skill_version_id=claim.skill_version_id,
            task_key=claim.task_key,
            actor_id=claim.created_by,
            input_json=claim.input_json,
            sources=claim.sources,
        ).to_json(),
        "definition": definition_json(claim.definition),
        "exhausted": claim.exhausted,
        "missed": claim.missed,
        "configuration_version": claim.configuration_version,
        "claim_row_version": claim.claim_row_version,
    }
    if (
        row.snapshot_checksum != claim.snapshot_checksum
        or row.occurrence_at != claim.occurrence_at
        or canonical_json(original) != canonical_json(candidate)
    ):
        raise ScheduleOccurrenceConflictError("Schedule claim does not match original snapshot")


def _require_fence(
    row: TaskScheduleOccurrence,
    claim: ClaimedSchedule,
    *,
    now: datetime,
    pending_write: bool = False,
) -> None:
    """原結算の読取確認以外は、lock 後にも同じ有効 lease を必須にする。"""

    if (
        row.status not in {"PENDING", "SETTLED"}
        or row.worker_id != claim.worker_id
        or type(claim.lease_generation) is not int
        or row.lease_generation != claim.lease_generation
        or row.lease_expires_at != claim.lease_expires_at
        or not hmac.compare_digest(row.lease_token_hash, lease_token_hash(claim.lease_token))
        or ((row.status == "PENDING" or pending_write) and row.lease_expires_at <= now)
    ):
        raise ScheduleClaimLostError("Schedule claim is no longer current")


def _validate_result(row: TaskScheduleOccurrence, result: ScheduleTriggerResult) -> None:
    """終態の型と scope を検証し、任意の Run や架空の完了を関連付けない。"""

    if (
        result.schedule_id != row.schedule_id
        or result.occurrence_at != row.occurrence_at
        or result.outcome
        not in {
            ScheduleOutcome.RUN_CREATED,
            ScheduleOutcome.SKIPPED_OVERLAP,
            ScheduleOutcome.FAILED_PRECONDITION,
        }
        or (result.outcome is ScheduleOutcome.RUN_CREATED) != isinstance(result.run_id, UUID)
        or (result.run_id is not None and not isinstance(result.run_id, UUID))
        or (
            result.detail is not None
            and (not isinstance(result.detail, str) or len(result.detail) > 512)
        )
    ):
        raise ScheduleOccurrenceConflictError("Occurrence result is invalid")


def _result(row: TaskScheduleOccurrence) -> ScheduleTriggerResult:
    """保存済みの結算だけを投影し、時刻や outcome を補わない。"""

    if row.outcome is None or row.settled_at is None:
        raise ScheduleOccurrenceConflictError("Stored occurrence settlement is invalid")
    result = ScheduleTriggerResult(
        row.schedule_id, row.occurrence_at, ScheduleOutcome(row.outcome), row.run_id, row.detail
    )
    _validate_result(row, result)
    return result
