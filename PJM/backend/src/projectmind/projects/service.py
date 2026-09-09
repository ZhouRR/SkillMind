"""Project CRUD と membership authorization の transaction 境界を提供する。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.service import AuthenticatedActor
from projectmind.projects.domain import (
    CreateProjectCommand,
    ProjectMemberNotFoundError,
    ProjectMemberStatus,
    ProjectMemberUserNotFoundError,
    ProjectPermissionDeniedError,
    StoredProject,
    StoredProjectMember,
    StoredProjectPreference,
    UpdateProjectCommand,
)
from projectmind.projects.repository import ProjectRepository
from projectmind.users.access import authorize_user_access, validate_user_access
from projectmind.users.domain import UserAccess
from projectmind.users.repository import LockedUsers, UserRepository


class ProjectService:
    """Actor の system role と membership を適用して Project use case を実行する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Database session factory を保持する。"""

        self._session_factory = session_factory

    async def list_projects(
        self,
        *,
        actor: AuthenticatedActor,
        include_archived: bool,
    ) -> tuple[StoredProject, ...]:
        """Actor が参照可能な Project 一覧だけを返す。"""

        async with self._session_factory() as session:
            return await ProjectRepository(session).list_accessible(
                actor=actor,
                include_archived=include_archived,
            )

    async def get_project(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
    ) -> StoredProject:
        """Project access を fail closed で検証して metadata を返す。"""

        async with self._session_factory() as session:
            return await ProjectRepository(session).get_accessible(
                actor=actor,
                project_id=project_id,
            )

    async def get_preference(self, *, actor: AuthenticatedActor) -> StoredProjectPreference:
        """Actor の現在も有効な Project preference を返す。"""

        async with self._session_factory() as session:
            return await ProjectRepository(session).get_preference(actor=actor)

    async def set_preference(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID | None,
    ) -> StoredProjectPreference:
        """Actor 自身の認可済み Project preference を更新する。"""

        async with self._session_factory() as session, session.begin():
            return await ProjectRepository(session).set_preference(
                actor=actor,
                project_id=project_id,
            )

    async def create_project(
        self,
        *,
        actor: AuthenticatedActor,
        key: str,
        name: str,
        description: str,
        settings: dict[str, object],
        retention_days: int,
    ) -> StoredProject:
        """ADMIN の Organization 内に ACTIVE Project を作成する。"""

        self._require_admin(actor)
        command = CreateProjectCommand(
            organization_id=actor.organization_id,
            key=key,
            name=name,
            description=description,
            settings=settings,
            retention_days=retention_days,
        )
        async with self._session_factory() as session, session.begin():
            return await ProjectRepository(session).create(command)

    async def update_project(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
        command: UpdateProjectCommand,
    ) -> StoredProject:
        """ADMIN が変更可能な Project metadata だけを更新する。"""

        self._require_admin(actor)
        async with self._session_factory() as session, session.begin():
            return await ProjectRepository(session).update(
                organization_id=actor.organization_id,
                project_id=project_id,
                command=command,
            )

    async def archive_project(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
    ) -> StoredProject:
        """ADMIN が Project を物理削除せず ARCHIVED にする。"""

        self._require_admin(actor)
        async with self._session_factory() as session, session.begin():
            return await ProjectRepository(session).archive(
                organization_id=actor.organization_id,
                project_id=project_id,
            )

    async def unarchive_project(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
    ) -> StoredProject:
        """ADMIN が ARCHIVED Project を ACTIVE へ戻す。"""

        self._require_admin(actor)
        async with self._session_factory() as session, session.begin():
            return await ProjectRepository(session).unarchive(
                organization_id=actor.organization_id,
                project_id=project_id,
            )

    async def delete_project(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
    ) -> None:
        """ADMIN が Run/Schedule/所属監査のない ARCHIVED Project を単一 transaction で削除する。"""

        self._require_admin(actor)
        async with self._session_factory() as session, session.begin():
            await ProjectRepository(session).delete(
                organization_id=actor.organization_id,
                project_id=project_id,
            )

    async def list_members(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
    ) -> tuple[StoredProjectMember, ...]:
        """ADMIN に限り Project membership を返す。"""

        async with self._member_transaction(access, write=False) as (repository, locked):
            result = await repository.list_members(
                organization_id=locked.actor.organization_id,
                project_id=project_id,
            )
            self._authorize_member(access, locked, write=False)
            return result

    async def add_member(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        user_id: UUID,
    ) -> StoredProjectMember:
        """ADMIN に限り ACTIVE User の membership を追加または再有効化する。"""

        async with self._member_transaction(access, target_id=user_id, write=True) as (
            repository, locked,
        ):
            member = await repository.lock_member(
                organization_id=locked.actor.organization_id, project_id=project_id,
                user_id=user_id, active_project=True,
            )
            now = self._authorize_member(access, locked, write=True)
            user = locked.target
            if user is None or user.status != "ACTIVE":
                raise ProjectMemberUserNotFoundError(f"Active User not found: {user_id}")
            return await repository.add_member(
                project_id=project_id, user=user, member=member,
                actor_id=locked.actor.id, request_id=access.request_id, now=now,
            )

    async def remove_member(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        user_id: UUID,
    ) -> None:
        """ADMIN に限り membership を REMOVED へ遷移する。"""

        async with self._member_transaction(access, target_id=user_id, write=True) as (
            repository, locked,
        ):
            member = await repository.lock_member(
                organization_id=locked.actor.organization_id, project_id=project_id,
                user_id=user_id, active_project=False,
            )
            now = self._authorize_member(access, locked, write=True)
            if (
                locked.target is None or member is None
                or member.status != ProjectMemberStatus.ACTIVE.value
            ):
                raise ProjectMemberNotFoundError(f"Active ProjectMember not found: {user_id}")
            repository.remove_member(
                organization_id=locked.actor.organization_id, member=member,
                actor_id=locked.actor.id, request_id=access.request_id, now=now,
            )

    @asynccontextmanager
    async def _member_transaction(
        self, access: UserAccess, *, target_id: UUID | None = None, write: bool,
    ) -> AsyncIterator[tuple[ProjectRepository, LockedUsers]]:
        """User 管理と同じ Org→User→Session を持ち、所属と監査を一緒に commit する。"""

        self._require_admin(access.actor)
        validate_user_access(access)
        async with self._session_factory() as session, session.begin():
            locked = await UserRepository(session).lock_users(
                access=access, target_id=target_id, include_target_sessions=False,
            )
            self._authorize_member(access, locked, write=write)
            yield ProjectRepository(session), locked
            # FK/監査 INSERT の失敗も期限切れも transaction 全体を失敗させる。
            await session.flush()
            self._authorize_member(access, locked, write=write)

    @staticmethod
    def _authorize_member(access: UserAccess, locked: LockedUsers, *, write: bool) -> datetime:
        """全資源の待機後にも users と共通の原 credential 検証を実行する。"""

        return authorize_user_access(
            access, locked, now=datetime.now(UTC), admin=True, write=write,
        )

    @staticmethod
    def _require_admin(actor: AuthenticatedActor) -> None:
        """Project 定義と membership の mutation を system ADMIN に限定する。"""

        if actor.system_role != "ADMIN":
            raise ProjectPermissionDeniedError("Administrator access is required")
