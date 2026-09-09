"""Project CRUD と membership authorization の transaction 境界を提供する。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.service import AuthenticatedActor
from projectmind.projects.domain import (
    CreateProjectCommand,
    ProjectPermissionDeniedError,
    StoredProject,
    StoredProjectMember,
    StoredProjectPreference,
    UpdateProjectCommand,
)
from projectmind.projects.repository import ProjectRepository


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
        """ADMIN が Run/Schedule のない ARCHIVED Project を一つの transaction で削除する。"""

        self._require_admin(actor)
        async with self._session_factory() as session, session.begin():
            await ProjectRepository(session).delete(
                organization_id=actor.organization_id,
                project_id=project_id,
            )

    async def list_members(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
    ) -> tuple[StoredProjectMember, ...]:
        """ADMIN に限り Project membership を返す。"""

        self._require_admin(actor)
        async with self._session_factory() as session:
            return await ProjectRepository(session).list_members(
                organization_id=actor.organization_id,
                project_id=project_id,
            )

    async def add_member(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
        user_id: UUID,
    ) -> StoredProjectMember:
        """ADMIN に限り ACTIVE User の membership を追加または再有効化する。"""

        self._require_admin(actor)
        async with self._session_factory() as session, session.begin():
            return await ProjectRepository(session).add_member(
                organization_id=actor.organization_id,
                project_id=project_id,
                user_id=user_id,
            )

    async def remove_member(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
        user_id: UUID,
    ) -> None:
        """ADMIN に限り membership を REMOVED へ遷移する。"""

        self._require_admin(actor)
        async with self._session_factory() as session, session.begin():
            await ProjectRepository(session).remove_member(
                organization_id=actor.organization_id,
                project_id=project_id,
                user_id=user_id,
            )

    @staticmethod
    def _require_admin(actor: AuthenticatedActor) -> None:
        """Project 定義と membership の mutation を system ADMIN に限定する。"""

        if actor.system_role != "ADMIN":
            raise ProjectPermissionDeniedError("Administrator access is required")
