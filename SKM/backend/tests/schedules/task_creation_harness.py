"""実 Schedule participant と Run/Skill repository を同一 SQL transaction fake へ接続する。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy import Select, select

from skillmind.db.models import Run, TaskSchedule, TaskScheduleOccurrence, User
from skillmind.runs.domain import TERMINAL_RUN_STATUSES, CreatedRun
from skillmind.schedules.creation import ScheduleRunCreationParticipant
from skillmind.schedules.domain import ClaimedSchedule
from skillmind.schedules.repository_occurrences import _claimed
from tests.runs.creation_authorization_harness import CreationAuthorizationHarness
from tests.runs.creation_fakes import creation_command, stored_creation
from tests.schedules.fakes import TOKEN, occurrence_row, schedule_row


class ScheduledTaskCreationHarness(CreationAuthorizationHarness):
    """持久認領も fake callback で通さず、本物の lock/fence/結算に保存行を渡す。"""

    def __init__(self, *, replay: bool = False) -> None:
        """Run の原要求と Schedule の不変 occurrence を同一 identity で準備する。"""

        super().__init__("first-replay" if replay else "new")
        self.schedule = schedule_row()
        self.schedule.project_id = self.project.id
        self.schedule.created_by = self.user.id
        self.schedule.skill_version_id = self.version.id
        self.schedule.task_key = self.intent.task_key
        self.schedule.input_json = self.intent.input_json
        self.schedule.sources_json = self.intent.sources
        self.occurrence = occurrence_row(self.schedule)
        self.schedule.row_version = 2
        self.key = self.occurrence.idempotency_key
        self.winner = stored_creation(
            replace(creation_command(self.intent), idempotency_key=self.key)
        )
        self.committed = [self.schedule, self.occurrence] + ([self.winner] if replay else [])

    @property
    def claim(self) -> ClaimedSchedule:
        """保存された原認領から共有 codec で DTO を作り、原 token を明示する。"""

        return _claimed(self.occurrence, token=TOKEN)

    async def scalar(self, statement: Select[Any]) -> Any:
        """原 User/認領/重複 SQL だけ追加し、他の未知 query は親の厳格拒否へ渡す。"""

        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        if entity is User:
            self.query(statement)
            assert set(params) == {"id_1"}
            if statement.column_descriptions[0]["expr"] is User.organization_id:
                assert statement.compare(
                    select(User.organization_id).where(User.id == params["id_1"])
                )
                return self.user.organization_id if self.user.id == params["id_1"] else None
            assert statement.compare(
                select(User).where(User.id == params["id_1"]).with_for_update(read=True)
            )
            assert statement.get_execution_options()["populate_existing"] is True
            return self.user if self.user.id == params["id_1"] else None
        if entity in (TaskSchedule, TaskScheduleOccurrence):
            self.query(statement)
            assert statement.get_execution_options()["populate_existing"] is True
            if entity is TaskSchedule:
                assert set(params) == {"project_id_1", "id_1"}
                expected = (
                    select(TaskSchedule)
                    .where(
                        TaskSchedule.id == params["id_1"],
                        TaskSchedule.project_id == params["project_id_1"],
                    )
                    .with_for_update()
                )
                assert statement.compare(expected)
                return (
                    self.schedule
                    if (
                        self.schedule.id == params["id_1"]
                        and self.schedule.project_id == params["project_id_1"]
                    )
                    else None
                )
            assert set(params) == {"id_1", "schedule_id_1"}
            expected_occurrence = (
                select(TaskScheduleOccurrence)
                .where(
                    TaskScheduleOccurrence.id == params["id_1"],
                    TaskScheduleOccurrence.schedule_id == params["schedule_id_1"],
                )
                .with_for_update()
            )
            assert statement.compare(expected_occurrence)
            return (
                self.occurrence
                if (
                    self.occurrence.id == params["id_1"]
                    and self.occurrence.schedule_id == params["schedule_id_1"]
                )
                else None
            )
        if entity is Run:
            self.query(statement)
            assert set(params) == {"schedule_id_1", "id_1", "status_1", "param_1"}
            expected_overlap = (
                select(Run.id)
                .join(TaskScheduleOccurrence, TaskScheduleOccurrence.run_id == Run.id)
                .where(
                    TaskScheduleOccurrence.schedule_id == params["schedule_id_1"],
                    TaskScheduleOccurrence.id != params["id_1"],
                    Run.status.not_in(tuple(item.value for item in TERMINAL_RUN_STATUSES)),
                )
                .limit(1)
            )
            assert statement.compare(expected_overlap)
            # この fixture は元 occurrence 一件のみ。自分の仮 INSERT を overlap に数えない。
            return None
        return await super().scalar(statement)

    async def call(self) -> CreatedRun:
        """実 participant の資格・fence・結算で普通 Run 作成 use case を呼ぶ。"""

        return await self.service.create_task_run(
            project_id=self.project.id,
            resolved=self.resolved,
            input_json=self.intent.input_json,
            sources=self.intent.sources,
            idempotency_key=self.key,
            trace_id=None,
            actor_id=self.user.id,
            authorization=ScheduleRunCreationParticipant(self.claim),
        )
