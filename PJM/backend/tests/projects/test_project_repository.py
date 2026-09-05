"""ProjectRepository の隔離、soft archive、membership 不変条件を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.auth.service import AuthenticatedActor
from projectmind.db.models import Project, ProjectMember, User
from projectmind.projects.domain import (
    CreateProjectCommand,
    ProjectDeleteBlockedError,
    ProjectMemberStatus,
    ProjectMemberUserNotFoundError,
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
async def test_project_preference_accepts_only_accessible_active_project() -> None:
    """Preference 更新が actor の認可済み ACTIVE Project だけを保存する。"""

    actor = _actor()
    user = _user(actor)
    project = _project(organization_id=actor.organization_id)
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[user, project])

    stored = await ProjectRepository(session).set_preference(
        actor=actor,
        project_id=project.id,
    )

    assert stored.project_id == project.id
    assert user.preferred_project_id == project.id


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

    first = await ProjectRepository(session).archive(
        organization_id=actor.organization_id,
        project_id=project.id,
    )
    second = await ProjectRepository(session).archive(
        organization_id=actor.organization_id,
        project_id=project.id,
    )

    assert first.status is ProjectStatus.ARCHIVED
    assert second.status is ProjectStatus.ARCHIVED
    assert not hasattr(session, "delete") or session.delete.call_count == 0


@pytest.mark.asyncio
async def test_add_member_rejects_user_outside_actor_organization() -> None:
    """別 Organization または無効 User を membership に追加しない。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id)
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[project, None])

    with pytest.raises(ProjectMemberUserNotFoundError):
        await ProjectRepository(session).add_member(
            organization_id=actor.organization_id,
            project_id=project.id,
            user_id=uuid4(),
        )
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
    session.scalar = AsyncMock(side_effect=[project, member])

    await ProjectRepository(session).remove_member(
        organization_id=actor.organization_id,
        project_id=project.id,
        user_id=member.user_id,
    )

    assert member.status == ProjectMemberStatus.REMOVED.value
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

    stored = await ProjectRepository(session).unarchive(
        organization_id=actor.organization_id,
        project_id=project.id,
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
        await ProjectRepository(session).delete(
            organization_id=actor.organization_id,
            project_id=project.id,
        )

    assert raised.value.blockers == ("project_not_archived",)
    session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_is_blocked_while_run_history_exists() -> None:
    """Run が残る Project を消させず、監査の正本を削除で失わないようにする。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[project, 3])

    with pytest.raises(ProjectDeleteBlockedError) as raised:
        await ProjectRepository(session).delete(
            organization_id=actor.organization_id,
            project_id=project.id,
        )

    assert raised.value.blockers == ("run_history_exists",)
    session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_clears_preference_and_owned_configuration_rows() -> None:
    """Run のない ARCHIVED Project は preference と設定 row ごと削除できる。"""

    actor = _actor(role="ADMIN")
    project = _project(organization_id=actor.organization_id, status="ARCHIVED")
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[project, 0])
    session.execute = AsyncMock()
    session.delete = AsyncMock()

    await ProjectRepository(session).delete(
        organization_id=actor.organization_id,
        project_id=project.id,
    )

    # preference の解除 1 回 + 設定 model ごとの削除。最後に Project 本体を消す。
    assert session.execute.await_count == 1 + len(_PROJECT_OWNED_MODELS)
    session.delete.assert_awaited_once_with(project)
