"""ProjectRepository の隔離、soft archive、membership 不変条件を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.auth.service import AuthenticatedActor
from projectmind.db.models import Project, ProjectMember, ProjectMemberEvent, TaskSchedule, User
from projectmind.projects.domain import (
    CreateProjectCommand,
    ProjectDeleteBlockedError,
    ProjectMemberStatus,
    ProjectNotFoundError,
    ProjectStatus,
)
from projectmind.projects.repository import _PROJECT_OWNED_MODELS, ProjectRepository


def _actor(*, role: str = "USER") -> AuthenticatedActor:
    """Repository test 用の認証済み actor を生成する。"""

    return AuthenticatedActor(
        user_id=uuid4(),
        organization_id=uuid4(),
        email="user@example.com",
        display_name="User",
        system_role=role,
    )


def _project(*, organization_id: object, status: str = "ACTIVE") -> Project:
    """Repository test 用の Project row を生成する。"""

    from uuid import UUID

    assert isinstance(organization_id, UUID)
    now = datetime.now(UTC)
    return Project(
        id=uuid4(),
        organization_id=organization_id,
        key="quality-team",
        name="Quality Team",
        description="description",
        status=status,
        settings_json={},
        retention_days=90,
        row_version=1,
        created_at=now,
        updated_at=now,
    )


def _user(actor: AuthenticatedActor, *, preferred_project_id: object = None) -> User:
    """Project preference test 用の ACTIVE User row を生成する。"""

    from uuid import UUID

    assert preferred_project_id is None or isinstance(preferred_project_id, UUID)
    now = datetime.now(UTC)
    return User(
        id=actor.user_id,
        organization_id=actor.organization_id,
        email=actor.email,
        password_hash="hash",
        display_name=actor.display_name,
        system_role=actor.system_role,
        status="ACTIVE",
        last_login_at=None,
        preferred_project_id=preferred_project_id,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_user_project_lookup_fails_closed_when_membership_is_not_active() -> None:
    """Project 存在有無に関係なく無所属 USER の lookup を同じ not found に畳み込む。"""

    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=None)

    with pytest.raises(ProjectNotFoundError):
        await ProjectRepository(session).get_accessible(actor=_actor(), project_id=uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ADMIN", "USER"])
async def test_detail_query_retains_organization_and_membership_without_archive_filter(
    role: str,
) -> None:
    """SQL 構造上の組織/所属制約と履歴読取を検証し、実 DB 認可の証明とは区別する。"""

    actor = _actor(role=role)
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=project)

    stored = await ProjectRepository(session).get_accessible(actor=actor, project_id=project.id)

    statement = session.scalar.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    predicates = str(statement.whereclause)
    assert stored.status is ProjectStatus.ARCHIVED
    assert "projects.id =" in predicates
    assert "projects.organization_id =" in predicates
    assert actor.organization_id in compiled.params.values()
    assert project.id in compiled.params.values()
    assert "projects.status" not in predicates
    if role == "USER":
        assert "project_members.status =" in predicates
        assert "project_members.user_id =" in predicates
        assert actor.user_id in compiled.params.values()
        assert "ACTIVE" in compiled.params.values()
    else:
        assert "project_members" not in predicates


@pytest.mark.asyncio
async def test_project_preference_accepts_only_accessible_active_project() -> None:
    """Preference 更新が actor の認可済み ACTIVE Project だけを保存する。"""

    actor = _actor()
    user = _user(actor)
    project = _project(organization_id=actor.organization_id)
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[object(), user, project])

    stored = await ProjectRepository(session).set_preference(
        actor=actor,
        project_id=project.id,
    )

    assert stored.project_id == project.id
    assert user.preferred_project_id == project.id
    assert project.row_version == 1 and project.updated_at == project.created_at
    statements = [str(call.args[0]) for call in session.scalar.call_args_list]
    assert "FROM organizations" in statements[0] and "FOR UPDATE" in statements[0]
    assert "FROM users" in statements[1] and "FOR UPDATE" in statements[1]
    assert "FROM projects" in statements[2]


@pytest.mark.asyncio
async def test_stale_project_preference_is_hidden_after_access_is_removed() -> None:
    """Membership 解除後の preference が Project の存在を漏らさず未選択になる。"""

    actor = _actor()
    project_id = uuid4()
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[_user(actor, preferred_project_id=project_id), None])

    stored = await ProjectRepository(session).get_preference(actor=actor)

    assert stored.project_id is None


@pytest.mark.asyncio
async def test_create_project_sets_active_status_and_keeps_key_immutable() -> None:
    """新規 Project が ACTIVE で作られ、更新対象とは別の固定 key を保持する。"""

    actor = _actor(role="ADMIN")
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    command = CreateProjectCommand(
        organization_id=actor.organization_id,
        key="quality-team",
        name="Quality Team",
        description="description",
        settings={"timezone": "Asia/Tokyo"},
        retention_days=90,
    )

    stored = await ProjectRepository(session).create(command)

    added = session.add.call_args.args[0]
    assert isinstance(added, Project)
    assert stored.key == "quality-team"
    assert stored.status is ProjectStatus.ACTIVE
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_archive_is_soft_and_idempotent() -> None:
    """Project archive が row を削除せず反復可能な状態変更になる。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id)
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=project)

    first = ProjectRepository(session).set_status(
        project=project, status=ProjectStatus.ARCHIVED,
        expected_row_version=1, now=datetime.now(UTC),
    )
    second = ProjectRepository(session).set_status(
        project=project, status=ProjectStatus.ARCHIVED,
        expected_row_version=2, now=datetime.now(UTC),
    )

    assert first.status is ProjectStatus.ARCHIVED
    assert second.status is ProjectStatus.ARCHIVED
    assert not hasattr(session, "delete") or session.delete.call_count == 0


@pytest.mark.asyncio
async def test_member_lock_hides_missing_project_before_membership_lookup() -> None:
    """別 Organization の Project は同じ 404 となり、所属検索へ進まない。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id)
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=None)

    with pytest.raises(ProjectNotFoundError):
        await ProjectRepository(session).lock_member(
            organization_id=actor.organization_id,
            project_id=project.id,
            user_id=uuid4(),
            active_project=True,
        )
    session.scalar.assert_awaited_once()
    query = session.scalar.call_args.args[0].compile(dialect=postgresql.dialect())
    assert set(query.params.values()) == {actor.organization_id, project.id}
    assert "FOR UPDATE" in str(query)
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_remove_member_preserves_row_as_removed() -> None:
    """Membership 解除が監査 row を削除せず REMOVED 状態へ遷移する。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id)
    now = datetime.now(UTC)
    member = ProjectMember(
        id=uuid4(),
        project_id=project.id,
        user_id=uuid4(),
        status=ProjectMemberStatus.ACTIVE.value,
        joined_at=now,
        created_at=now,
        updated_at=now,
    )
    session = MagicMock(spec=AsyncSession)
    request_id = uuid4()
    ProjectRepository(session).remove_member(
        organization_id=actor.organization_id,
        member=member, actor_id=actor.user_id, request_id=request_id, now=now,
    )

    assert member.status == ProjectMemberStatus.REMOVED.value
    event = session.add.call_args.args[0]
    assert isinstance(event, ProjectMemberEvent)
    assert event.previous_status == "ACTIVE" and event.status == "REMOVED"
    assert event.previous_joined_at == event.joined_at == now
    assert event.actor_id == actor.user_id and event.request_id == request_id
    assert not hasattr(session, "delete") or session.delete.call_count == 0


@pytest.mark.asyncio
async def test_member_projection_never_exposes_password_hash() -> None:
    """Membership read model が User credential を公開 field に含めない。"""

    now = datetime.now(UTC)
    user = User(
        id=uuid4(),
        organization_id=uuid4(),
        email="user@example.com",
        password_hash="secret-hash",
        display_name="User",
        system_role="USER",
        status="ACTIVE",
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )
    member = ProjectMember(
        id=uuid4(),
        project_id=uuid4(),
        user_id=user.id,
        status="ACTIVE",
        joined_at=now,
        created_at=now,
        updated_at=now,
    )

    stored = ProjectRepository._to_stored_member(member, user)

    assert stored.email == "user@example.com"
    assert not hasattr(stored, "password_hash")


@pytest.mark.asyncio
async def test_unarchive_restores_archived_project_without_changing_key() -> None:
    """復元が ACTIVE へ戻すだけで、key を作り直させないことを確認する。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=project)

    stored = ProjectRepository(session).set_status(
        project=project, status=ProjectStatus.ACTIVE,
        expected_row_version=1, now=datetime.now(UTC),
    )

    assert stored.status is ProjectStatus.ACTIVE
    assert stored.key == "quality-team"


@pytest.mark.asyncio
async def test_delete_requires_archived_status() -> None:
    """ACTIVE Project の削除を拒否し、archive を経ない key 解放を塞ぐ。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id)
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=project)

    with pytest.raises(ProjectDeleteBlockedError) as raised:
        await ProjectRepository(session).delete(project=project, expected_row_version=1)

    assert raised.value.blockers == ("project_not_archived",)
    session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_is_blocked_while_run_history_exists() -> None:
    """Run が残る Project を消させず、監査の正本を削除で失わないようにする。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[3])

    with pytest.raises(ProjectDeleteBlockedError) as raised:
        await ProjectRepository(session).delete(project=project, expected_row_version=1)

    assert raised.value.blockers == ("run_history_exists",)
    session.delete.assert_not_called()
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_delete_rejects_any_schedule_before_removing_preference_or_configuration() -> None:
    """Schedule は状態・発火実績・認領値で除外せず、設定変更より先に参照拒否する。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[0, True])

    with pytest.raises(ProjectDeleteBlockedError) as raised:
        await ProjectRepository(session).delete(project=project, expected_row_version=1)

    assert raised.value.blockers == ("task_schedule_exists",)
    schedule_query = session.scalar.call_args_list[1].args[0]
    compiled = schedule_query.compile(dialect=postgresql.dialect())
    assert "EXISTS" in str(compiled)
    assert "FROM task_schedules" in str(compiled)
    assert "task_schedules.project_id =" in str(compiled)
    assert list(compiled.params.values()) == [project.id]
    for excluded_filter in ("status", "run_count", "last_run_id", "next_run_at"):
        assert excluded_filter not in str(compiled)
    assert TaskSchedule not in _PROJECT_OWNED_MODELS
    assert {fk.ondelete for fk in TaskSchedule.__table__.c.project_id.foreign_keys} == {"RESTRICT"}
    session.execute.assert_not_called()
    session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_lookup_database_error_is_not_reported_as_an_existing_schedule() -> None:
    """DB 障害は参照有無を証明しないため、業務拒否へ誤変換せず transaction へ戻す。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    failure = IntegrityError("test query", {}, RuntimeError("test database failure"))
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[0, failure])

    with pytest.raises(IntegrityError) as raised:
        await ProjectRepository(session).delete(project=project, expected_row_version=1)

    assert raised.value is failure
    session.execute.assert_not_called()
    session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_clears_preference_and_owned_configuration_rows() -> None:
    """Run/Schedule のない ARCHIVED Project は preference と設定 row ごと削除できる。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[0, False, False])
    session.execute = AsyncMock()
    session.delete = AsyncMock()

    await ProjectRepository(session).delete(project=project, expected_row_version=1)

    # preference の解除 1 回 + 設定 model ごとの削除。最後に Project 本体を消す。
    assert session.execute.await_count == 1 + len(_PROJECT_OWNED_MODELS)
    session.delete.assert_awaited_once_with(project)


@pytest.mark.asyncio
async def test_member_audit_blocks_delete_before_any_writes() -> None:
    """取得済み Project の所属監査を key 解放のために消さず、設定変更も開始しない。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[0, False, True])
    with pytest.raises(ProjectDeleteBlockedError) as raised:
        await ProjectRepository(session).delete(project=project, expected_row_version=1)
    assert raised.value.blockers == ("member_audit_exists",)
    statements = [str(call.args[0]) for call in session.scalar.call_args_list]
    assert "EXISTS" in statements[2] and "project_member_events.project_id =" in statements[2]
    assert ProjectMemberEvent not in _PROJECT_OWNED_MODELS
    session.execute.assert_not_called()
    session.delete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("active_project", [True, False])
async def test_member_lock_checks_project_status_only_for_add(active_project: bool) -> None:
    """Archive は追加を 404 にするが、解除のための所属取得は残す。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[project, None])
    repository = ProjectRepository(session)
    if active_project:
        with pytest.raises(ProjectNotFoundError):
            await repository.lock_member(
                organization_id=actor.organization_id, project_id=project.id,
                user_id=uuid4(), active_project=True,
            )
        assert session.scalar.await_count == 1
    else:
        assert await repository.lock_member(
            organization_id=actor.organization_id, project_id=project.id,
            user_id=uuid4(), active_project=False,
        ) is None
        member_query = str(session.scalar.call_args_list[1].args[0])
        assert "FOR UPDATE" in member_query
        assert "project_members.status =" not in member_query


@pytest.mark.asyncio
async def test_member_list_projects_all_relationship_states_without_account_status() -> None:
    """既存一覧は REMOVED と無効 User の関係を隠さず、公開状態は membership のままにする。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    user = _user(actor)
    user.status = "DISABLED"
    member = ProjectMember(
        id=uuid4(), project_id=project.id, user_id=user.id, status="REMOVED",
        joined_at=user.created_at, created_at=user.created_at, updated_at=user.updated_at,
    )
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=project)
    rows = MagicMock()
    rows.all.return_value = [(member, user)]
    session.execute = AsyncMock(return_value=rows)
    result = await ProjectRepository(session).list_members(
        organization_id=actor.organization_id, project_id=project.id,
    )
    assert result[0].status == "REMOVED"
    query = str(session.execute.call_args.args[0])
    assert "LIMIT" not in query and "OFFSET" not in query
    assert "users.status =" not in query and "project_members.status =" not in query
    assert "users.organization_id =" in query
    compiled = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert set(compiled.params.values()) == {actor.organization_id, project.id}
