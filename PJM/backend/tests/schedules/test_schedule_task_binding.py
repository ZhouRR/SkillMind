"""調度保存にも現在の精確版 gate を適用し、停止操作と SQL 待機の優先順位を守る。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import uuid4

import pytest

from projectmind.auth.sessions import UnauthorizedSessionError
from projectmind.db.models import Project, ProjectSkillVersion, SkillVersion, TaskSchedule
from projectmind.projects.domain import ProjectNotFoundError
from projectmind.schedules.domain import (
    ScheduleConflictError,
    ScheduleInvalidError,
    ScheduleNotFoundError,
    ScheduleStatus,
)
from tests.runs.creation_authorization_harness import row_values
from tests.runs.test_creation_authorization import CreationClock
from tests.schedules.authorization_harness import ScheduleAuthorizationDatabase


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("change", ["disabled", "deprecated", "source-org", "foreign-project"])
async def test_schedule_save_rechecks_binding_after_outside_resolution(
    operation: str, change: str
) -> None:
    """鎖外 resolve が成功しても、組織 gate 前に commit した停用/廃棄を保存へ持ち込まない。"""

    db = ScheduleAuthorizationDatabase()
    before = row_values(db.schedule)

    def change_after_resolve() -> None:
        """実並行 DB を主張せず、解析後・保存 TX 前の確定順序を作る。"""

        assert not db.in_transaction
        assert db.task_binding.binding is not None
        if change == "disabled":
            db.task_binding.binding.disabled_at = datetime.now(UTC)
        elif change == "deprecated":
            db.task_binding.version.status = "DEPRECATED"
        elif change == "source-org":
            db.task_binding.source.organization_id = uuid4()
        else:
            db.task_binding.binding.project_id = uuid4()

    db.on_resolve = change_after_resolve
    with pytest.raises(ScheduleInvalidError, match=r"^Published task is not available$"):
        await db.submit(operation)
    assert row_values(db.schedule) == before
    assert len(db.schedules) == 1 and db.occurrences == [] and db.runs == []
    assert db.commits == 0 and db.rollbacks == 1
    db.session.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", [ScheduleStatus.ACTIVE, ScheduleStatus.PAUSED, ScheduleStatus.ARCHIVED]
)
async def test_only_reactivation_requires_a_current_task(target: ScheduleStatus) -> None:
    """廃棄済み task の停止/帰档は残し、再開だけを現在の有効化へ束縛する。"""

    db = ScheduleAuthorizationDatabase()
    db.schedule.status = "PAUSED" if target is ScheduleStatus.ACTIVE else "ACTIVE"
    db.task_binding.version.status = "DEPRECATED"
    original = row_values(db.schedule)
    change_status = partial(
        db.service.change_status,
        project_id=db.project.id,
        schedule_id=db.schedule.id,
        access=db.access,
        target=target,
        expected_row_version=db.schedule.row_version,
    )
    if target is ScheduleStatus.ACTIVE:
        with pytest.raises(ScheduleInvalidError, match="Published task is not available"):
            await change_status()
        assert row_values(db.schedule) == original
    else:
        result = await change_status()
        assert result.status is target
        assert SkillVersion not in db.lock_events
        assert ProjectSkillVersion not in db.lock_events


@pytest.mark.asyncio
async def test_reactivation_checks_original_cas_before_task_availability() -> None:
    """原版不一致を今日の task 不在に読み替えず、比較・再読取の契約を維持する。"""

    db = ScheduleAuthorizationDatabase()
    db.schedule.status = "PAUSED"
    db.task_binding.binding = None
    with pytest.raises(ScheduleConflictError):
        await db.service.change_status(
            project_id=db.project.id,
            schedule_id=db.schedule.id,
            access=db.access,
            target=ScheduleStatus.ACTIVE,
            expected_row_version=db.schedule.row_version + 1,
        )
    assert SkillVersion not in db.lock_events


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("available", [False, True])
async def test_schedule_binding_wait_revalidates_original_session(
    monkeypatch: pytest.MonkeyPatch, operation: str, available: bool
) -> None:
    """新 guard の成功・拒否とも、固定期限を跨いだ原会話より先に Task 状態を返さない。"""

    db = ScheduleAuthorizationDatabase()
    before = row_values(db.schedule)
    CreationClock.current = datetime.now(UTC)
    db.auth_session.idle_expires_at = CreationClock.current + timedelta(seconds=1)
    monkeypatch.setattr("projectmind.schedules.service.datetime", CreationClock)
    if not available:
        db.task_binding.binding = None

    def advance(entity: type[Any]) -> None:
        """期限行の書換えでなく SQL 待機後の時計だけを進める。"""

        if entity is ProjectSkillVersion:
            CreationClock.current += timedelta(seconds=2)

    db.on_lock = advance
    with pytest.raises(UnauthorizedSessionError):
        await db.submit(operation)
    assert row_values(db.schedule) == before
    assert db.rollbacks == 1 and db.commits == 0


@pytest.mark.asyncio
async def test_reactivation_lock_order_uses_version_share_after_schedule_update() -> None:
    """認領の FK KEY SHARE と互換な順序を、実共有 guard の二つの SQL で通す。"""

    db = ScheduleAuthorizationDatabase()
    db.schedule.status = "PAUSED"
    await db.service.change_status(
        project_id=db.project.id,
        schedule_id=db.schedule.id,
        access=db.access,
        target=ScheduleStatus.ACTIVE,
        expected_row_version=db.schedule.row_version,
    )
    assert db.lock_events[-4:] == [TaskSchedule, SkillVersion, ProjectSkillVersion, TaskSchedule]


@pytest.mark.asyncio
async def test_expired_guard_success_precedes_edit_of_newly_completed_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """鎖外読取後に完了した調度でも、guard 待機中の会話失効を業務拒否より先に返す。"""

    db = ScheduleAuthorizationDatabase()
    CreationClock.current = datetime.now(UTC)
    db.auth_session.idle_expires_at = CreationClock.current + timedelta(seconds=1)
    monkeypatch.setattr("projectmind.schedules.service.datetime", CreationClock)

    def complete_before_transaction() -> None:
        """他 writer の変更は組織 gate 取得前に置き、持鎖中の競争を捏造しない。"""

        assert not db.in_transaction
        db.schedule.status = "COMPLETED"

    def advance(entity: type[Any]) -> None:
        """保存された会話 expiry は固定し、新 gate の正常応答までに時刻を進める。"""

        if entity is ProjectSkillVersion:
            CreationClock.current += timedelta(seconds=2)

    db.on_resolve = complete_before_transaction
    db.on_lock = advance
    with pytest.raises(UnauthorizedSessionError):
        await db.submit("edit")
    # 二度目の repository 編集 lock/終態判断へ到達するより前に拒否する。
    assert db.lock_events.count(TaskSchedule) == 1
    assert db.schedule.status == "COMPLETED" and db.schedule.row_version == 1
    assert db.rollbacks == 1 and db.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["project", "schedule"])
@pytest.mark.parametrize("expired", [False, True])
async def test_missing_target_after_lock_wait_checks_original_session_first(
    monkeypatch: pytest.MonkeyPatch, target: str, expired: bool
) -> None:
    """helper の不存在早期出口でも、新時刻の会話拒否を越えて 404 を公開しない。"""

    db = ScheduleAuthorizationDatabase()
    CreationClock.current = datetime.now(UTC)
    db.auth_session.idle_expires_at = CreationClock.current + timedelta(seconds=1)
    monkeypatch.setattr("projectmind.schedules.service.datetime", CreationClock)
    if target == "project":
        db.project_present = False
    else:
        db.schedules.clear()

    def advance(entity: type[Any]) -> None:
        """行を途中で変えず、対象 SELECT の待機後だけ固定期限を跨ぐ。"""

        if expired and entity is (Project if target == "project" else TaskSchedule):
            CreationClock.current += timedelta(seconds=2)

    db.on_lock = advance
    expected = (
        UnauthorizedSessionError
        if expired
        else (ProjectNotFoundError if target == "project" else ScheduleNotFoundError)
    )
    with pytest.raises(expected):
        await db.submit("status")
    assert db.rollbacks == 1 and db.commits == 0
    db.session.flush.assert_not_awaited()
    assert SkillVersion not in db.lock_events
