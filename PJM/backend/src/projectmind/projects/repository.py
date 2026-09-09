"""Project metadata と membership の PostgreSQL 永続化を実装する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.auth.service import AuthenticatedActor
from projectmind.db.models import (
    EffectPreauthorization,
    Integration,
    ManagedSecretMaterial,
    Project,
    ProjectComposition,
    ProjectDocument,
    ProjectMember,
    ProjectSkillVersion,
    ResourceBinding,
    Run,
    SecretReference,
    TaskSchedule,
    User,
)
from projectmind.projects.domain import (
    CreateProjectCommand,
    ProjectDeleteBlockedError,
    ProjectKeyConflictError,
    ProjectMemberNotFoundError,
    ProjectMemberStatus,
    ProjectMemberUserNotFoundError,
    ProjectNotFoundError,
    ProjectStatus,
    StoredProject,
    StoredProjectMember,
    StoredProjectPreference,
    UpdateProjectCommand,
)

# Project 削除時に一緒に消す設定 row を FK の葉から根の順で並べる。
# Integration は SecretReference を、ResourceBinding と EffectPreauthorization は
# Integration を参照するため、この順序を崩すと RESTRICT で削除が失敗する。
_PROJECT_OWNED_MODELS = (
    ManagedSecretMaterial,
    EffectPreauthorization,
    ResourceBinding,
    ProjectSkillVersion,
    ProjectComposition,
    ProjectDocument,
    ProjectMember,
    Integration,
    SecretReference,
)


class ProjectRepository:
    """Transaction 内で Project isolation と membership 更新を行う。"""

    def __init__(self, session: AsyncSession) -> None:
        """Transaction-scoped database session を保持する。"""

        self._session = session

    async def list_accessible(
        self,
        *,
        actor: AuthenticatedActor,
        include_archived: bool,
    ) -> tuple[StoredProject, ...]:
        """ADMIN は組織内、USER は有効 membership の Project だけを返す。"""

        statement = select(Project).where(Project.organization_id == actor.organization_id)
        if actor.system_role != "ADMIN":
            statement = statement.where(
                exists().where(
                    ProjectMember.project_id == Project.id,
                    ProjectMember.user_id == actor.user_id,
                    ProjectMember.status == ProjectMemberStatus.ACTIVE.value,
                )
            )
        if not include_archived:
            statement = statement.where(Project.status == ProjectStatus.ACTIVE.value)
        projects = (
            await self._session.scalars(statement.order_by(Project.name, Project.id))
        ).all()
        return tuple(self._to_stored(project) for project in projects)

    async def get_accessible(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
    ) -> StoredProject:
        """Project の存在と actor の参照権限を一つの 404 境界へ畳み込む。"""

        project = await self._accessible_model(actor=actor, project_id=project_id)
        return self._to_stored(project)

    async def get_preference(self, *, actor: AuthenticatedActor) -> StoredProjectPreference:
        """保存済み preference が現在も認可済み ACTIVE Project の場合だけ返す。"""

        user = await self._actor_user(actor=actor, lock=False)
        if user.preferred_project_id is None:
            return StoredProjectPreference(project_id=None)
        try:
            project = await self._accessible_model(
                actor=actor,
                project_id=user.preferred_project_id,
            )
        except ProjectNotFoundError:
            # Membership 解除後の stale preference から Project の存在を漏らさない。
            return StoredProjectPreference(project_id=None)
        if project.status != ProjectStatus.ACTIVE.value:
            return StoredProjectPreference(project_id=None)
        return StoredProjectPreference(project_id=project.id)

    async def set_preference(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID | None,
    ) -> StoredProjectPreference:
        """認可済み ACTIVE Project または未選択を User preference に保存する。"""

        user = await self._actor_user(actor=actor, lock=True)
        if project_id is not None:
            project = await self._accessible_model(actor=actor, project_id=project_id)
            if project.status != ProjectStatus.ACTIVE.value:
                raise ProjectNotFoundError(f"Active Project not found: {project_id}")
        user.preferred_project_id = project_id
        user.updated_at = datetime.now(UTC)
        return StoredProjectPreference(project_id=project_id)

    async def create(self, command: CreateProjectCommand) -> StoredProject:
        """Organization 内で key が一意な ACTIVE Project を作成する。"""

        existing = await self._session.scalar(
            select(Project.id).where(
                Project.organization_id == command.organization_id,
                Project.key == command.key,
            )
        )
        if existing is not None:
            raise ProjectKeyConflictError(f"Project key already exists: {command.key}")
        now = datetime.now(UTC)
        project = Project(
            id=uuid4(),
            organization_id=command.organization_id,
            key=command.key,
            name=command.name,
            description=command.description,
            status=ProjectStatus.ACTIVE.value,
            settings_json=command.settings,
            retention_days=command.retention_days,
            created_at=now,
            updated_at=now,
        )
        self._session.add(project)
        try:
            # 事前確認と insert の競合も public conflict へ変換するため flush まで行う。
            await self._session.flush()
        except IntegrityError as error:
            raise ProjectKeyConflictError(f"Project key already exists: {command.key}") from error
        return self._to_stored(project)

    async def update(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        command: UpdateProjectCommand,
    ) -> StoredProject:
        """Project key と status を変えず、許可された metadata だけを更新する。"""

        project = await self._organization_project(
            organization_id=organization_id,
            project_id=project_id,
            lock=True,
        )
        if command.name is not None:
            project.name = command.name
        if command.description is not None:
            project.description = command.description
        if command.settings is not None:
            project.settings_json = command.settings
        if command.retention_days is not None:
            project.retention_days = command.retention_days
        project.updated_at = datetime.now(UTC)
        return self._to_stored(project)

    async def archive(self, *, organization_id: UUID, project_id: UUID) -> StoredProject:
        """監査履歴を削除せず Project を冪等に ARCHIVED へ遷移する。"""

        project = await self._organization_project(
            organization_id=organization_id,
            project_id=project_id,
            lock=True,
        )
        if project.status != ProjectStatus.ARCHIVED.value:
            project.status = ProjectStatus.ARCHIVED.value
            project.updated_at = datetime.now(UTC)
        return self._to_stored(project)

    async def unarchive(self, *, organization_id: UUID, project_id: UUID) -> StoredProject:
        """ARCHIVED Project を冪等に ACTIVE へ戻す。

        key は archive 時も解放しないため、同じ key の新規作成が 409 になった利用者は
        この復元で元の Project へ戻れる。設定と membership は archive で失っていない。
        """

        project = await self._organization_project(
            organization_id=organization_id,
            project_id=project_id,
            lock=True,
        )
        if project.status != ProjectStatus.ACTIVE.value:
            project.status = ProjectStatus.ACTIVE.value
            project.updated_at = datetime.now(UTC)
        return self._to_stored(project)

    async def delete(self, *, organization_id: UUID, project_id: UUID) -> None:
        """Run/Schedule を持たない ARCHIVED Project と、その設定 row を物理削除する。

        key の一意制約は status を区別しないため、archive しただけでは key を再利用できない。
        ここは「作成し直したい」用途のための唯一の解放手段であり、監査の正本である Run が
        一件でもあれば削除しない。未発火や認領中の Schedule も将来の実行参照なので残す。
        設定削除は全ての検査後に同一 transaction で行い、FK RESTRICT を最後の防壁に保つ。
        """

        project = await self._organization_project(
            organization_id=organization_id,
            project_id=project_id,
            lock=True,
        )
        if project.status != ProjectStatus.ARCHIVED.value:
            raise ProjectDeleteBlockedError(
                f"Project must be archived before deletion: {project_id}",
                blockers=("project_not_archived",),
            )
        run_count = await self._session.scalar(
            select(func.count()).select_from(Run).where(Run.project_id == project_id)
        )
        if run_count:
            raise ProjectDeleteBlockedError(
                f"Project still has {run_count} run(s): {project_id}",
                blockers=("run_history_exists",),
            )
        # status、発火実績、next_run_at による絞込は、停止済み・未発火・認領中の参照を漏らす。
        has_schedule = await self._session.scalar(
            select(exists().where(TaskSchedule.project_id == project_id))
        )
        if has_schedule:
            raise ProjectDeleteBlockedError(
                f"Project still has task schedule references: {project_id}",
                blockers=("task_schedule_exists",),
            )
        # Preference は FK RESTRICT なので、参照している User を先に未選択へ戻す。
        await self._session.execute(
            update(User)
            .where(User.preferred_project_id == project_id)
            .values(preferred_project_id=None)
        )
        # ResourceBinding の自己参照 (source_binding_id) と run_id は RUN scope snapshot だけが
        # 持ち、それは Run 作成時にしか生まれない。Run 零件を先に確認済みなので、ここでは
        # 自己参照 RESTRICT を踏まずに一括削除できる。
        for model in _PROJECT_OWNED_MODELS:
            await self._session.execute(delete(model).where(model.project_id == project_id))
        await self._session.delete(project)

    async def list_members(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
    ) -> tuple[StoredProjectMember, ...]:
        """同一 Organization の Project について membership と公開 User 情報を返す。"""

        await self._organization_project(
            organization_id=organization_id,
            project_id=project_id,
            lock=False,
        )
        rows = (
            await self._session.execute(
                select(ProjectMember, User)
                .join(User, User.id == ProjectMember.user_id)
                .where(ProjectMember.project_id == project_id)
                .order_by(User.display_name, User.id)
            )
        ).all()
        return tuple(self._to_stored_member(member, user) for member, user in rows)

    async def add_member(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        user_id: UUID,
    ) -> StoredProjectMember:
        """同一 Organization の ACTIVE User を追加し、REMOVED membership は再有効化する。"""

        project = await self._organization_project(
            organization_id=organization_id,
            project_id=project_id,
            lock=True,
        )
        if project.status != ProjectStatus.ACTIVE.value:
            raise ProjectNotFoundError(f"Active Project not found: {project_id}")
        user = await self._session.scalar(
            select(User).where(
                User.id == user_id,
                User.organization_id == organization_id,
                User.status == "ACTIVE",
            )
        )
        if user is None:
            raise ProjectMemberUserNotFoundError(f"Active User not found: {user_id}")
        member = await self._session.scalar(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
            .with_for_update()
        )
        now = datetime.now(UTC)
        if member is None:
            member = ProjectMember(
                id=uuid4(),
                project_id=project_id,
                user_id=user_id,
                status=ProjectMemberStatus.ACTIVE.value,
                joined_at=now,
                created_at=now,
                updated_at=now,
            )
            self._session.add(member)
        elif member.status != ProjectMemberStatus.ACTIVE.value:
            member.status = ProjectMemberStatus.ACTIVE.value
            member.joined_at = now
            member.updated_at = now
        return self._to_stored_member(member, user)

    async def remove_member(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        user_id: UUID,
    ) -> None:
        """Membership row を削除せず REMOVED にして監査可能性を維持する。"""

        await self._organization_project(
            organization_id=organization_id,
            project_id=project_id,
            lock=True,
        )
        member = await self._session.scalar(
            select(ProjectMember)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.status == ProjectMemberStatus.ACTIVE.value,
            )
            .with_for_update()
        )
        if member is None:
            raise ProjectMemberNotFoundError(f"Active ProjectMember not found: {user_id}")
        member.status = ProjectMemberStatus.REMOVED.value
        member.updated_at = datetime.now(UTC)

    async def _accessible_model(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
    ) -> Project:
        """Resource existence を漏らさず actor が参照できる Project model を取得する。"""

        statement = select(Project).where(
            Project.id == project_id,
            Project.organization_id == actor.organization_id,
        )
        if actor.system_role != "ADMIN":
            statement = statement.where(
                exists().where(
                    ProjectMember.project_id == Project.id,
                    ProjectMember.user_id == actor.user_id,
                    ProjectMember.status == ProjectMemberStatus.ACTIVE.value,
                )
            )
        project = await self._session.scalar(statement)
        if project is None:
            raise ProjectNotFoundError(f"Project not found: {project_id}")
        return project

    async def _organization_project(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        lock: bool,
    ) -> Project:
        """ADMIN mutation を actor の Organization 内 Project に制限する。"""

        statement = select(Project).where(
            Project.id == project_id,
            Project.organization_id == organization_id,
        )
        if lock:
            statement = statement.with_for_update()
        project = await self._session.scalar(statement)
        if project is None:
            raise ProjectNotFoundError(f"Project not found: {project_id}")
        return project

    async def _actor_user(self, *, actor: AuthenticatedActor, lock: bool) -> User:
        """Session actor と同一 Organization の ACTIVE User row を取得する。"""

        statement = select(User).where(
            User.id == actor.user_id,
            User.organization_id == actor.organization_id,
            User.status == "ACTIVE",
        )
        if lock:
            statement = statement.with_for_update()
        user = await self._session.scalar(statement)
        if user is None:
            raise ProjectNotFoundError("Authenticated user was not found")
        return user

    @staticmethod
    def _to_stored(project: Project) -> StoredProject:
        """Project ORM row を公開 read model へ変換する。"""

        return StoredProject(
            project_id=project.id,
            key=project.key,
            name=project.name,
            description=project.description,
            status=ProjectStatus(project.status),
            settings=dict(project.settings_json),
            retention_days=project.retention_days,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    @staticmethod
    def _to_stored_member(member: ProjectMember, user: User) -> StoredProjectMember:
        """Password credential を除外して membership response を生成する。"""

        return StoredProjectMember(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            status=ProjectMemberStatus(member.status),
            joined_at=member.joined_at,
        )
