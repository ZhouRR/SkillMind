"""TaskSchedule の永続化を実装する (計画 §22)。

到期 schedule の認領は専用 lease column を持たず、`next_run_at` の CAS
(`UPDATE ... WHERE id = ? AND next_run_at = ?`) で行う。認領と同時に次回候補へ進めるため、
発火が失敗しても同じ時刻を掴み直して無限にリトライする状態にならない。二重発火の最終的な
防止は §22 D7 の決定的 idempotency key が担う。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.auth.service import AuthenticatedActor
from projectmind.db.models import TaskSchedule, User
from projectmind.schedules.domain import (
    CreateScheduleCommand,
    ScheduleConflictError,
    ScheduleKind,
    ScheduleNotFoundError,
    ScheduleOutcome,
    SchedulePage,
    ScheduleRecord,
    ScheduleStatus,
    UpdateScheduleCommand,
)

# 発火可能なのは ACTIVE だけ。PAUSED / ERROR / COMPLETED / ARCHIVED は走査に入れない。
_DUE_STATUS = ScheduleStatus.ACTIVE.value


class ScheduleRepository:
    """Transaction-scoped session 上で schedule を読み書きする。"""

    def __init__(self, session: AsyncSession) -> None:
        """Transaction-scoped database session を保持する。"""

        self._session = session

    async def create(self, command: CreateScheduleCommand) -> ScheduleRecord:
        """schedule 行を追加し、公開投影を返す。"""

        now = datetime.now(UTC)
        row = TaskSchedule(
            id=uuid4(),
            project_id=command.project_id,
            name=command.name,
            kind=command.definition.kind.value,
            status=ScheduleStatus.ACTIVE.value,
            timezone=command.definition.timezone,
            cron_expression=command.definition.cron_expression,
            run_at=command.definition.run_at,
            end_at=command.definition.end_at,
            max_runs=command.definition.max_runs,
            skill_version_id=command.skill_version_id,
            task_key=command.task_key,
            input_json=dict(command.input_json),
            sources_json=dict(command.sources),
            next_run_at=command.next_run_at,
            run_count=0,
            missed_count=0,
            created_by=command.created_by,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_record(row)

    async def get(self, *, project_id: UUID, schedule_id: UUID) -> ScheduleRecord:
        """Project 境界内の schedule を取得する。越境と不存在は同じ例外へ畳む。"""

        return _to_record(await self._require_row(project_id=project_id, schedule_id=schedule_id))

    async def list_for_project(
        self, *, project_id: UUID, limit: int, offset: int
    ) -> SchedulePage:
        """Project 内 schedule を次回発火時刻の昇順で列挙する。"""

        total = int(
            await self._session.scalar(
                select(func.count())
                .select_from(TaskSchedule)
                .where(TaskSchedule.project_id == project_id)
            )
            or 0
        )
        statement = (
            select(TaskSchedule)
            .where(TaskSchedule.project_id == project_id)
            # 発火待ちを先頭に出す。NULL (発火予定なし) は末尾へ送る。
            .order_by(
                TaskSchedule.next_run_at.is_(None),
                TaskSchedule.next_run_at,
                TaskSchedule.created_at,
            )
            .limit(limit)
            .offset(offset)
        )
        rows = list(await self._session.scalars(statement))
        return SchedulePage(
            items=tuple(_to_record(row) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    async def update_definition(self, command: UpdateScheduleCommand) -> ScheduleRecord:
        """定義と凍結入力を差し替える。row_version 不一致は衝突として拒否する。"""

        row = await self._require_row(
            project_id=command.project_id, schedule_id=command.schedule_id
        )
        if row.row_version != command.expected_row_version:
            raise ScheduleConflictError("Schedule was modified by another request")
        row.name = command.name
        row.kind = command.definition.kind.value
        row.timezone = command.definition.timezone
        row.cron_expression = command.definition.cron_expression
        row.run_at = command.definition.run_at
        row.end_at = command.definition.end_at
        row.max_runs = command.definition.max_runs
        row.input_json = dict(command.input_json)
        row.sources_json = dict(command.sources)
        row.next_run_at = command.next_run_at
        row.row_version += 1
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _to_record(row)

    async def set_status(
        self,
        *,
        project_id: UUID,
        schedule_id: UUID,
        status: ScheduleStatus,
        next_run_at: datetime | None,
        last_error: str | None = None,
    ) -> ScheduleRecord:
        """状態遷移の結果を書き込む。遷移の可否判定は service 側の状態機が担う。"""

        row = await self._require_row(project_id=project_id, schedule_id=schedule_id)
        row.status = status.value
        row.next_run_at = next_run_at
        row.last_error = last_error
        row.row_version += 1
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _to_record(row)

    async def list_due(self, *, now: datetime, limit: int) -> list[ScheduleRecord]:
        """発火時刻を過ぎた ACTIVE schedule を古い順に返す。"""

        statement = (
            select(TaskSchedule)
            .where(
                TaskSchedule.status == _DUE_STATUS,
                TaskSchedule.next_run_at.is_not(None),
                TaskSchedule.next_run_at <= now,
            )
            .order_by(TaskSchedule.next_run_at)
            .limit(limit)
        )
        return [_to_record(row) for row in await self._session.scalars(statement)]

    async def claim(
        self,
        *,
        schedule_id: UUID,
        expected_next_run_at: datetime,
        next_run_at: datetime | None,
        missed: int,
    ) -> bool:
        """`next_run_at` の CAS で認領する。他 worker に先を越されていれば False。

        認領時点で次回候補へ進めるので、発火処理そのものが落ちても同じ時刻を再走査しない。
        """

        statement = (
            update(TaskSchedule)
            .where(
                TaskSchedule.id == schedule_id,
                TaskSchedule.status == _DUE_STATUS,
                TaskSchedule.next_run_at == expected_next_run_at,
            )
            .values(
                next_run_at=next_run_at,
                missed_count=TaskSchedule.missed_count + missed,
                row_version=TaskSchedule.row_version + 1,
                updated_at=datetime.now(UTC),
            )
        )
        # UPDATE の影響行数がそのまま「自分が獲得したか」の答えになる。SQLAlchemy の型では
        # 汎用 Result が返るため、行数を持つ CursorResult へ絞り込む。
        result = cast(CursorResult[Any], await self._session.execute(statement))
        return result.rowcount == 1

    async def record_outcome(
        self,
        *,
        schedule_id: UUID,
        occurrence_at: datetime,
        outcome: ScheduleOutcome,
        run_id: UUID | None,
        detail: str | None,
        status: ScheduleStatus | None,
    ) -> None:
        """一回の発火結果を schedule 行へ書き戻す。

        `status` を渡したときだけ状態を変える。Run 生成の成否と状態遷移は別の判断なので、
        呼び出し側が明示したときにしか status を触らない。
        """

        values: dict[str, Any] = {
            "last_run_at": occurrence_at,
            "last_outcome": outcome.value,
            "last_error": detail,
            "row_version": TaskSchedule.row_version + 1,
            "updated_at": datetime.now(UTC),
        }
        if run_id is not None:
            values["last_run_id"] = run_id
            values["run_count"] = TaskSchedule.run_count + 1
        if status is not None:
            values["status"] = status.value
            if status is not ScheduleStatus.ACTIVE:
                # 発火を止める状態では次回予定を消す。残すと一覧で「次に走る」ように見える。
                values["next_run_at"] = None
        await self._session.execute(
            update(TaskSchedule).where(TaskSchedule.id == schedule_id).values(**values)
        )

    async def load_creator_actor(self, user_id: UUID) -> AuthenticatedActor | None:
        """schedule 作成者の現在の identity を返す。不存在・無効化済みなら None (§22 D8)。

        ここが返すのは identity だけで、Project への到達可否は判定しない。その判定は
        `ProjectRepository.get_accessible` という単一実装に委ねる——同じ意味の授権判定を
        二箇所に書くと、片方だけ緩む余地が残る。
        """

        row = await self._session.scalar(
            select(User).where(User.id == user_id, User.status == "ACTIVE")
        )
        if row is None:
            return None
        return AuthenticatedActor(
            user_id=row.id,
            organization_id=row.organization_id,
            email=row.email,
            display_name=row.display_name,
            system_role=row.system_role,
        )

    async def _require_row(self, *, project_id: UUID, schedule_id: UUID) -> TaskSchedule:
        """Project 境界内の行を取得する。越境は不存在と同じ扱いにする。"""

        statement = select(TaskSchedule).where(
            TaskSchedule.id == schedule_id,
            TaskSchedule.project_id == project_id,
        )
        row = await self._session.scalar(statement)
        if row is None:
            raise ScheduleNotFoundError("Schedule was not found")
        return row


def _to_record(row: TaskSchedule) -> ScheduleRecord:
    """ORM 行を公開投影へ写す。"""

    return ScheduleRecord(
        schedule_id=row.id,
        project_id=row.project_id,
        name=row.name,
        kind=ScheduleKind(row.kind),
        status=ScheduleStatus(row.status),
        timezone=row.timezone,
        cron_expression=row.cron_expression,
        run_at=row.run_at,
        end_at=row.end_at,
        max_runs=row.max_runs,
        skill_version_id=row.skill_version_id,
        task_key=row.task_key,
        input_json=dict(row.input_json or {}),
        sources=dict(row.sources_json or {}),
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        last_run_id=row.last_run_id,
        last_outcome=ScheduleOutcome(row.last_outcome) if row.last_outcome else None,
        last_error=row.last_error,
        run_count=row.run_count,
        missed_count=row.missed_count,
        created_by=row.created_by,
        row_version=row.row_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
