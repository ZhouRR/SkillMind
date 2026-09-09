"""三つの管理書込に原会話・現在資格・commit 後の返却を要求する局部回帰。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.elements import ColumnElement

from projectmind.auth.domain import generate_session_credentials, hash_session_secret
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.db.models import (
    AuthSession,
    Organization,
    Project,
    ProjectMember,
    TaskSchedule,
    TaskScheduleOccurrence,
    User,
)
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from projectmind.schedules.domain import ScheduleNotFoundError, ScheduleStatus
from tests.schedules.authorization_harness import ScheduleAuthorizationDatabase
from tests.schedules.fakes import occurrence_row, run_for_occurrence, schedule_row

OPERATIONS = ("create", "edit", "status")
AUTH_FAILURES = (
    "revoked",
    "disabled",
    "role",
    "role-aba",
    "idle",
    "absolute",
    "csrf",
    "token",
    "missing-user",
    "missing-session",
    "missing-organization",
    "actor-organization",
    "session-owner",
    "credential-version",
    "csrf-hash",
    "new-session-only",
)
PROJECT_FAILURES = (
    "missing-member",
    "removed-member",
    "member-user",
    "member-project",
    "project-organization",
    "missing-project",
    "archived",
)


class ScheduleClock:
    """期限値を変更せず、lock/flush の待機後に service が読む時刻だけを進める。"""

    current = datetime.now(UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        """同じ合成時刻を timezone を保って返す。"""

        return cls.current.astimezone(tz)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> type[ScheduleClock]:
    """実待機ではなく controllable clock を schedule service のみに注入する。"""

    ScheduleClock.current = datetime.now(UTC)
    monkeypatch.setattr("projectmind.schedules.service.datetime", ScheduleClock)
    return ScheduleClock


def business_snapshot(db: ScheduleAuthorizationDatabase) -> dict[str, Any]:
    """行内容と集合の両方を比較し、新 Schedule 参照の取り残しも見つける。"""

    return deepcopy(
        {
            "current": db.schedule.id,
            "schedules": [row_values(row) for row in db.schedules],
            "occurrences": [row_values(row) for row in db.occurrences],
            "runs": [row_values(row) for row in db.runs],
        }
    )


def row_values(row: Any) -> dict[str, Any]:
    """SQLAlchemy 内部 identity state は rollback 比較へ持ち込まない。"""

    return {key: value for key, value in vars(row).items() if key != "_sa_instance_state"}


def add_other_session(db: ScheduleAuthorizationDatabase) -> AuthSession:
    """同じ actor の新しい login が原 cookie を代行できないことを検証する。"""

    credentials = generate_session_credentials()
    new = AuthSession(
        **{
            **row_values(db.auth_session),
            "id": uuid4(),
            "token_hash": credentials.session_token_hash,
            "csrf_token_hash": credentials.csrf_token_hash,
            "revoked_at": None,
        }
    )
    db.auth_sessions.append(new)
    return new


def invalidate(db: ScheduleAuthorizationDatabase, failure: str) -> None:
    """外部で成立した失効事実を合成し、業務 rollback の対象とは区別する。"""

    now = datetime.now(UTC)
    if failure in {"revoked", "role-aba"}:
        db.auth_session.revoked_at = now
    elif failure == "disabled":
        db.user.status = "DISABLED"
    elif failure == "role":
        db.user.system_role = "ADMIN"
    elif failure == "idle":
        db.auth_session.idle_expires_at = now - timedelta(seconds=1)
    elif failure == "absolute":
        db.auth_session.absolute_expires_at = now - timedelta(seconds=1)
    elif failure == "csrf":
        db.access = replace(db.access, csrf_token="synthetic-wrong-proof")
    elif failure == "token":
        db.auth_session.token_hash = "sha256:synthetic-not-original"
    elif failure == "missing-user":
        db.users.clear()
    elif failure == "missing-session":
        db.auth_sessions.clear()
    elif failure == "missing-organization":
        db.organization = None
    elif failure == "actor-organization":
        db.access = replace(db.access, actor=replace(db.access.actor, organization_id=uuid4()))
    elif failure == "session-owner":
        db.auth_session.user_id = uuid4()
    elif failure == "credential-version":
        db.auth_session.credential_version = 1
    elif failure == "csrf-hash":
        db.auth_session.csrf_token_hash = hash_session_secret("synthetic-corrupt-hash")
    elif failure == "new-session-only":
        newer = add_other_session(db)
        db.auth_sessions[:] = [newer]
    elif failure == "missing-member":
        db.member = None
    elif failure == "removed-member":
        assert db.member is not None
        db.member.status = "REMOVED"
    elif failure == "member-user":
        assert db.member is not None
        db.member.user_id = uuid4()
    elif failure == "member-project":
        assert db.member is not None
        db.member.project_id = uuid4()
    elif failure == "project-organization":
        db.project.organization_id = uuid4()
    elif failure == "missing-project":
        db.project_present = False
    elif failure == "archived":
        db.project.status = "ARCHIVED"
    else:
        raise AssertionError(f"Unknown authorization failure: {failure}")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", AUTH_FAILURES)
async def test_each_management_write_requires_original_live_session(
    operation: str,
    failure: str,
) -> None:
    """入口 actor を信用せず、原 Session/CSRF と現在 user を実 validator で再確認する。"""

    db = ScheduleAuthorizationDatabase()
    original = business_snapshot(db)
    invalidate(db, failure)
    error = CsrfRejectedError if failure == "csrf" else UnauthorizedSessionError
    with pytest.raises(error):
        await db.submit(operation)
    assert business_snapshot(db) == original
    assert db.transactions == db.rollbacks == 1
    assert db.commits == 0
    db.session.add.assert_not_called()
    db.session.flush.assert_not_awaited()
    assert Project not in db.lock_events


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", PROJECT_FAILURES)
async def test_each_management_write_rechecks_project_and_membership(
    operation: str,
    failure: str,
) -> None:
    """所属・組織・帰档を同一 transaction の現在行で判定する。"""

    db = ScheduleAuthorizationDatabase()
    original = business_snapshot(db)
    invalidate(db, failure)
    error = ProjectArchivedError if failure == "archived" else ProjectNotFoundError
    with pytest.raises(error):
        await db.submit(operation)
    assert business_snapshot(db) == original
    assert db.rollbacks == 1 and db.commits == 0
    db.session.add.assert_not_called()
    db.session.flush.assert_not_awaited()
    assert TaskSchedule not in db.lock_events


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_removed_membership_precedes_archived_project_disclosure(operation: str) -> None:
    """読めない Project の帰档状態を 409 で公開しない。"""

    db = ScheduleAuthorizationDatabase()
    invalidate(db, "removed-member")
    invalidate(db, "archived")
    with pytest.raises(ProjectNotFoundError):
        await db.submit(operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("current_admin", [False, True])
async def test_membership_bypass_uses_current_role_not_entry_actor(
    operation: str,
    current_admin: bool,
) -> None:
    """古い actor.role の昇格/降格表記で現在 User と login snapshot を上書きしない。"""

    db = ScheduleAuthorizationDatabase()
    db.member = None
    role = "ADMIN" if current_admin else "USER"
    db.user.system_role = role
    db.auth_session.system_role_at_login = role
    db.access = replace(
        db.access,
        actor=replace(
            db.access.actor,
            system_role="USER" if current_admin else "ADMIN",
        ),
    )
    if current_admin:
        result = await db.submit(operation)
        assert result.created_by == db.user.id
        assert db.commits == 1
        assert ProjectMember not in db.lock_events
    else:
        with pytest.raises(ProjectNotFoundError):
            await db.submit(operation)
        assert ProjectMember in db.lock_events
        assert db.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_new_login_does_not_replace_or_reactivate_original_session(operation: str) -> None:
    """新 login が同 actor でも、失効済み原 cookie の書込を許さない。"""

    db = ScheduleAuthorizationDatabase()
    newer = add_other_session(db)
    db.auth_session.revoked_at = datetime.now(UTC)
    with pytest.raises(UnauthorizedSessionError):
        await db.submit(operation)
    session_reads = [
        query for query in db.statements if (query.column_descriptions[0]["entity"] is AuthSession)
    ]
    assert len(session_reads) == 1
    params = session_reads[0].compile().params
    assert params == {
        "user_id_1": db.user.id,
        "token_hash_1": hash_session_secret(db.access.session_token),
    }
    assert params["token_hash_1"] != newer.token_hash
    assert newer.revoked_at is None
    assert db.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("stage", [Organization, User, AuthSession, Project, ProjectMember])
@pytest.mark.parametrize("expiry", ["idle_expires_at", "absolute_expires_at"])
async def test_lock_wait_expiry_is_checked_with_fresh_time(
    operation: str,
    stage: type[Any],
    expiry: str,
    clock: type[ScheduleClock],
) -> None:
    """lock 待機前の有効判定ではなく、返った lock の後で期限を再判定する。"""

    db = ScheduleAuthorizationDatabase()
    before = business_snapshot(db)
    expires = clock.current + timedelta(seconds=1)
    setattr(db.auth_session, expiry, expires)

    def advance(entity: type[Any]) -> None:
        """指定 lock の待機中に時計だけを期限境界まで進める。"""

        if entity is stage:
            clock.current = expires

    db.on_lock = advance
    with pytest.raises(UnauthorizedSessionError):
        await db.submit(operation)
    assert getattr(db.auth_session, expiry) == expires
    assert business_snapshot(db) == before
    assert db.commits == 0 and db.rollbacks == 1
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["edit", "status"])
@pytest.mark.parametrize("expiry", ["idle_expires_at", "absolute_expires_at"])
async def test_schedule_lock_wait_rechecks_before_cas_or_business_changes(
    operation: str,
    expiry: str,
    clock: type[ScheduleClock],
) -> None:
    """Schedule の lock 待機でも session 期限が延命されない。"""

    db = ScheduleAuthorizationDatabase()
    before = business_snapshot(db)
    expires = clock.current + timedelta(seconds=1)
    setattr(db.auth_session, expiry, expires)

    def advance(entity: type[Any]) -> None:
        """業務 row を取った瞬間に期限を越える。"""

        if entity is TaskSchedule:
            clock.current = expires

    db.on_lock = advance
    with pytest.raises(UnauthorizedSessionError):
        await db.submit(operation)
    assert db.lock_events.count(TaskSchedule) == 1
    assert business_snapshot(db) == before
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("flush_number", [1, 2])
@pytest.mark.parametrize("failure", ["idle", "absolute", "revoked", "removed-member", "archived"])
async def test_flush_rejection_restores_rows_collections_and_original_schedule_reference(
    operation: str,
    flush_number: int,
    failure: str,
    clock: type[ScheduleClock],
) -> None:
    """追加/変更後に失効しても原行・全集合を戻し、最後の flush 待機も再確認する。"""

    db = ScheduleAuthorizationDatabase(claimed=True)
    extra = schedule_row()
    db.schedules.append(extra)
    db.runs.append(run_for_occurrence(db.occurrences[0]))
    original_schedule = db.schedule
    before = business_snapshot(db)
    expires = clock.current + timedelta(seconds=1)
    if failure in {"idle", "absolute"}:
        setattr(db.auth_session, f"{failure}_expires_at", expires)

    def fail_after_write() -> None:
        """通常なら commit へ進む二度目の flush も、独立した待機点として扱う。"""

        if db.session.flush.await_count != flush_number:
            return
        if failure in {"idle", "absolute"}:
            clock.current = expires
        else:
            invalidate(db, failure)

    db.on_flush = fail_after_write
    error: type[Exception] = UnauthorizedSessionError
    if failure == "removed-member":
        error = ProjectNotFoundError
    elif failure == "archived":
        error = ProjectArchivedError
    with pytest.raises(error):
        await db.submit(operation)
    assert db.session.flush.await_count >= flush_number
    assert db.schedule is original_schedule
    assert business_snapshot(db) == before
    assert db.transactions == db.rollbacks == 1
    assert db.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "edit"])
async def test_skill_and_input_resolution_stay_outside_authentication_locks(operation: str) -> None:
    """外部資源検証の間に権限が失効しても、後続の新 transaction がそれを見直す。"""

    db = ScheduleAuthorizationDatabase()
    before = business_snapshot(db)

    def revoke() -> None:
        """lock の外で進んだ認証状態を、後続の再認証へ見せる。"""

        assert db.transactions == 0
        assert db.lock_events == []
        invalidate(db, "revoked")

    db.on_resolve = revoke
    with pytest.raises(UnauthorizedSessionError):
        await db.submit(operation)
    db.skills.resolve_task_run.assert_awaited_once()
    db.runs_service.validate_task_sources.assert_awaited_once()
    assert business_snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_success_returns_only_after_commit_with_fixed_lock_order(operation: str) -> None:
    """repository の DTO 取得や flush 完了を commit の成功として返さない。"""

    db = ScheduleAuthorizationDatabase()
    original_id = db.schedule.id
    identity_before = deepcopy([row_values(db.user), row_values(db.auth_session)])
    db.commit_release = asyncio.Event()
    task = asyncio.create_task(db.submit(operation))
    try:
        await asyncio.wait_for(db.commit_entered.wait(), timeout=2)
        assert not task.done()
        assert db.commits == 0
        db.commit_release.set()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        db.commit_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert db.transactions == db.commits == 1
    assert [row_values(db.user), row_values(db.auth_session)] == identity_before
    assert db.rollbacks == 0
    assert result.created_by == db.user.id
    assert result.row_version == (1 if operation == "create" else 2)
    assert result.status is (
        ScheduleStatus.PAUSED if operation == "status" else ScheduleStatus.ACTIVE
    )
    assert (
        result.schedule_id != original_id
        if operation == "create"
        else result.schedule_id == original_id
    )
    assert db.session.flush.await_count == 2
    expected = [Organization, User, AuthSession, Project, ProjectMember]
    if operation != "create":
        expected += [TaskSchedule, TaskSchedule]
    assert db.lock_events == expected
    locked = [query for query in db.statements if query._for_update_arg is not None]
    assert [query.column_descriptions[0]["entity"] for query in locked] == expected
    dialect_factory: Callable[..., Dialect] = postgresql.dialect
    for query in locked:
        entity = query.column_descriptions[0]["entity"]
        sql = str(query.compile(dialect=dialect_factory()))
        assert sql.endswith(
            "FOR SHARE" if entity in {User, Project, ProjectMember} else "FOR UPDATE"
        )
        if entity is not Organization:
            assert query.get_execution_options()["populate_existing"] is True
    db.runs_service.create_task_run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("persists", [False, True])
async def test_commit_unknown_never_returns_success_or_retries(
    operation: str,
    persists: bool,
) -> None:
    """実際 rollback と実際 commit の両方で応答喪失を外へ返し、別操作を作らない。"""

    db = ScheduleAuthorizationDatabase()
    original_schedule = db.schedule
    before = business_snapshot(db)
    db.commit_error = True
    db.commit_persists = persists
    with pytest.raises(ConnectionError, match="commit acknowledgement"):
        await db.submit(operation)
    assert db.transactions == 1
    assert db.session.begin.call_count == 1
    assert db.commits == int(persists)
    assert db.rollbacks == int(not persists)
    assert db.session.flush.await_count == 2
    if persists:
        assert business_snapshot(db) != before
        assert len(db.schedules) == (2 if operation == "create" else 1)
        assert db.schedule.row_version == (1 if operation == "create" else 2)
    else:
        assert db.schedule is original_schedule
        assert business_snapshot(db) == before
    db.runs_service.create_task_run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["edit", "status"])
async def test_schedule_identity_is_project_scoped_without_mutation(operation: str) -> None:
    """同じ Schedule ID が別 Project に属していても管理できない。"""

    db = ScheduleAuthorizationDatabase()
    db.schedule.project_id = uuid4()
    before = business_snapshot(db)
    with pytest.raises(ScheduleNotFoundError):
        await db.submit(operation)
    assert business_snapshot(db) == before
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_fake_rollback_restores_all_existing_models_and_new_collections() -> None:
    """fake 自身が current 参照・全 Schedule/occurrence/Run の完全復元を行う。"""

    db = ScheduleAuthorizationDatabase(claimed=True)
    extra = schedule_row()
    db.schedules.append(extra)
    db.runs.append(run_for_occurrence(db.occurrences[0]))
    original = db.schedule
    before = business_snapshot(db)
    with pytest.raises(RuntimeError, match="rollback fixture"):
        async with db.transaction():
            extra.name = "Changed extra row"
            original.input_json["value"] = 9
            db.occurrences[0].attempt_count = 3
            db.runs[0].status = "FAILED"
            added = schedule_row()
            db.add(added)
            db.add(occurrence_row(added))
            db.runs.append(run_for_occurrence(db.occurrences[-1]))
            raise RuntimeError("rollback fixture")
    assert db.schedule is original
    assert business_snapshot(db) == before


@pytest.mark.asyncio
async def test_authorization_fake_rejects_unrecognized_sql_instead_of_granting_access() -> None:
    """未対応 SELECT や scope の欠落を暗黙成功にしない。"""

    db = ScheduleAuthorizationDatabase()
    with pytest.raises(AssertionError):
        await db.scalars(select(User))
    with pytest.raises(AssertionError):
        await db.scalars(select(User.email))
    with pytest.raises(AssertionError):
        await db.scalar(select(Project))


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", [User, AuthSession, Project, ProjectMember, TaskSchedule])
@pytest.mark.parametrize("malformation", ["or", "not-equal", "missing-scope"])
async def test_authentication_fake_does_not_replace_wrong_sql_with_its_own_authorization(
    entity: type[Any],
    malformation: str,
) -> None:
    """同じ bind parameter の OR/!= も拒否し、fake 側 AND で本番の誤りを隠さない。"""

    db = ScheduleAuthorizationDatabase()
    right: ColumnElement[bool]
    if entity is User:
        column, value = User.organization_id, db.user.organization_id
        right = User.id.in_([db.user.id])
    elif entity is AuthSession:
        column, value = AuthSession.user_id, db.user.id
        right = AuthSession.token_hash == db.auth_session.token_hash
    elif entity is Project:
        column, value = Project.id, db.project.id
        right = Project.organization_id == db.user.organization_id
    elif entity is ProjectMember:
        column, value = ProjectMember.project_id, db.project.id
        right = ProjectMember.user_id == db.user.id
    elif entity is TaskSchedule:
        column, value = TaskSchedule.id, db.schedule.id
        right = TaskSchedule.project_id == db.project.id
    else:
        raise AssertionError("Unexpected test entity")
    correct = and_(column == value, right)
    if malformation == "or":
        predicate = or_(column == value, right)
    elif malformation == "not-equal":
        predicate = and_(column != value, right)
    else:
        predicate = column == value
    statement = select(entity).where(predicate)
    if malformation != "missing-scope":
        assert statement.compile().params == select(entity).where(correct).compile().params
    query = db.scalars if entity in {User, AuthSession} else db.scalar
    with pytest.raises(AssertionError):
        await query(statement)


@pytest.mark.asyncio
async def test_organization_fake_requires_equality_not_merely_the_same_id_parameter() -> None:
    """組織 gate の否定条件を、同じ UUID だからという理由で通さない。"""

    db = ScheduleAuthorizationDatabase()
    assert db.organization is not None
    with pytest.raises(AssertionError):
        await db.scalar(select(Organization).where(Organization.id != db.organization.id))


@pytest.mark.asyncio
async def test_pending_count_uses_original_schedule_scope_and_rejects_wrong_predicate() -> None:
    """他 Schedule の PENDING を枠へ数えず、count の scope 消失も見逃さない。"""

    db = ScheduleAuthorizationDatabase(claimed=True)
    other = schedule_row()
    db.schedules.append(other)
    db.occurrences.append(occurrence_row(other))
    correct = and_(
        TaskScheduleOccurrence.schedule_id == db.schedule.id,
        TaskScheduleOccurrence.status == "PENDING",
    )
    statement = select(func.count()).select_from(TaskScheduleOccurrence).where(correct)
    assert await db.scalar(statement) == 1
    for predicate in (
        or_(
            TaskScheduleOccurrence.schedule_id == db.schedule.id,
            TaskScheduleOccurrence.status == "PENDING",
        ),
        and_(
            TaskScheduleOccurrence.schedule_id != db.schedule.id,
            TaskScheduleOccurrence.status == "PENDING",
        ),
        TaskScheduleOccurrence.status == "PENDING",
    ):
        with pytest.raises(AssertionError):
            await db.scalar(
                select(func.count()).select_from(TaskScheduleOccurrence).where(predicate)
            )
    with pytest.raises(AssertionError):
        await db.scalar(select(func.count()).select_from(User))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("stage", ["first-flush", "final-flush", "commit-exit"])
async def test_cancel_during_flush_or_transaction_exit_propagates_without_retry(
    operation: str,
    stage: str,
) -> None:
    """実 task cancel は外へ伝播し、fake commit 前の待機を成功や再試行へ変換しない。"""

    db = ScheduleAuthorizationDatabase()
    original = db.schedule
    before = business_snapshot(db)
    entered = asyncio.Event()
    release = asyncio.Event()
    if stage == "commit-exit":
        entered = db.commit_entered
        db.commit_release = release
    else:
        target_flush = 1 if stage == "first-flush" else 2

        async def hold_flush() -> None:
            """指定 flush が返る前に取消を配送する。"""

            if db.session.flush.await_count == target_flush:
                entered.set()
                await release.wait()

        db.session.flush.side_effect = hold_flush
    task = asyncio.create_task(db.submit(operation))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert db.schedule is original
    assert business_snapshot(db) == before
    assert db.transactions == db.rollbacks == 1
    assert db.commits == 0
    assert not db.in_transaction
    db.runs_service.create_task_run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["edit", "status"])
async def test_authorized_non_creator_does_not_replace_original_task_or_occurrence(
    operation: str,
) -> None:
    """現在の編集者で認可しても、原作成者/精確 task/認領済み入力を改変しない。"""

    db = ScheduleAuthorizationDatabase(claimed=True)
    creator = db.schedule.created_by
    task_identity = (db.schedule.skill_version_id, db.schedule.task_key)
    occurrences_before = deepcopy([row_values(row) for row in db.occurrences])
    db.user.id = uuid4()
    db.auth_session.user_id = db.user.id
    assert db.member is not None
    db.member.user_id = db.user.id
    db.access = replace(db.access, actor=replace(db.access.actor, user_id=db.user.id))
    identity_before = deepcopy([row_values(db.user), row_values(db.auth_session)])
    result = await db.submit(operation)
    assert db.commits == 1
    assert result.created_by == creator != db.user.id
    assert (result.skill_version_id, result.task_key) == task_identity
    assert [row_values(row) for row in db.occurrences] == occurrences_before
    assert [row_values(db.user), row_values(db.auth_session)] == identity_before
