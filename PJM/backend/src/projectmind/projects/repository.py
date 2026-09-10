"""Project metadata と membership の PostgreSQL 永続化を実装する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.auth.service import AuthenticatedActor
from projectmind.core.hashing import canonical_json
from projectmind.db.errors import matches_constraint
from projectmind.db.models import (
    EffectPreauthorization,
    Integration,
    ManagedSecretMaterial,
    Project,
    ProjectComposition,
    ProjectDocument,
    ProjectDocumentCleanup,
    ProjectDocumentUpload,
    ProjectMember,
    ProjectMemberEvent,
    ProjectSkillVersion,
    ResourceBinding,
    Run,
    SecretReference,
    TaskSchedule,
    User,
)
from projectmind.projects.domain import (
    CreateProjectCommand,
    ProjectArchivedError,
    ProjectDeleteBlockedError,
    ProjectKeyConflictError,
    ProjectMemberAction,
    ProjectMemberStatus,
    ProjectNotFoundError,
    ProjectStatus,
    StoredProject,
    StoredProjectMember,
    StoredProjectPreference,
    UpdateProjectCommand,
    next_project_version,
    require_project_version,
)
from projectmind.users.repository import lock_organization

# Project 削除時に一緒に消す設定 row を FK の葉から根の順で並べる。
# Integration は SecretReference を、ResourceBinding と EffectPreauthorization は
# Integration を参照するため、この順序を崩すと RESTRICT で削除が失敗する。
_PROJECT_OWNED_MODELS = (
    ManagedSecretMaterial,
    EffectPreauthorization,
    ResourceBinding,
    ProjectSkillVersion,
    ProjectComposition,
    ProjectMember,
    Integration,
    SecretReference,
)


@dataclass(frozen=True, slots=True)
class LockedProjectAccess:
    """同一 transaction 内で固定した現在の利用者・Project・所属を保持する。"""

    user: User = field(repr=False)
    project: Project = field(repr=False)
    member: ProjectMember | None = field(repr=False)


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

    async def lock_write_access(
        self, *, user: User, project_id: UUID
    ) -> LockedProjectAccess:
        """Org→User→Session の後、Run より前に現在の Project と所属を固定する。"""

        # Worker は Run を保持して Project FK の KEY SHARE を取得する。
        # FOR UPDATE では逆順待ちになるため、FK と両立し更新を止める SHARE を使う。
        project = await self._session.scalar(
            select(Project)
            .where(Project.id == project_id, Project.organization_id == user.organization_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise ProjectNotFoundError("Project not found")
        member = None
        if user.system_role != "ADMIN":
            member = await self._session.scalar(
                select(ProjectMember)
                .where(ProjectMember.project_id == project.id, ProjectMember.user_id == user.id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        return LockedProjectAccess(user=user, project=project, member=member)

    @staticmethod
    def require_active_write_access(locked: LockedProjectAccess) -> None:
        """所属の 404 境界を先に検査し、認可済み Project の帰档だけを 409 にする。"""

        ProjectRepository.require_read_access(locked)
        if locked.project.status != ProjectStatus.ACTIVE.value:
            raise ProjectArchivedError("Archived projects cannot be modified")

    @staticmethod
    def require_read_access(locked: LockedProjectAccess) -> None:
        """原要求の読取にも現在の組織と所属を要求し、帰档は履歴閲覧を妨げない。"""

        user, project, member = locked.user, locked.project, locked.member
        if project.organization_id != user.organization_id or (
            user.system_role != "ADMIN"
            and (
                member is None
                or member.project_id != project.id
                or member.user_id != user.id
                or member.status != ProjectMemberStatus.ACTIVE.value
            )
        ):
            raise ProjectNotFoundError("Project not found")

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

        # User→Project FK と削除の Project→User が交差する前に同じ gate を取得する。
        await lock_organization(self._session, actor.organization_id)
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
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(project)
        try:
            # 事前確認と insert の競合も public conflict へ変換するため flush まで行う。
            await self._session.flush()
        except IntegrityError as error:
            if matches_constraint(error, "uq_projects_organization_key"):
                raise ProjectKeyConflictError("Project key already exists") from error
            raise
        return self._to_stored(project)

    async def lock_project(self, *, organization_id: UUID, project_id: UUID) -> Project:
        """共有 Org→User→Session の取得後、同組織の Project を最終の資源 lock として固定する。"""

        return await self._organization_project(
            organization_id=organization_id, project_id=project_id, lock=True,
        )

    def update(
        self,
        *,
        project: Project,
        command: UpdateProjectCommand,
        now: datetime,
    ) -> StoredProject:
        """Project key と status を変えず、許可された metadata だけを更新する。"""

        require_project_version(project.row_version, command.expected_row_version)
        changed = any(value is not None and value != current for value, current in (
            (command.name, project.name),
            (command.description, project.description),
            (command.retention_days, project.retention_days),
        ))
        # Python の dict 等値では true と 1 が同じになるため、JSON の型を保って比較する。
        if command.settings is not None:
            changed = changed or (
                canonical_json(command.settings) != canonical_json(project.settings_json)
            )
        if not changed:
            return self._to_stored(project)
        # 上限検査より先にどの metadata も更新しない。
        next_version = next_project_version(project.row_version)
        if command.name is not None:
            project.name = command.name
        if command.description is not None:
            project.description = command.description
        if command.settings is not None:
            project.settings_json = command.settings
        if command.retention_days is not None:
            project.retention_days = command.retention_days
        project.row_version = next_version
        project.updated_at = now
        return self._to_stored(project)

    def set_status(
        self, *, project: Project, status: ProjectStatus, expected_row_version: int, now: datetime,
    ) -> StoredProject:
        """原版比較後に実遷移だけを増版し、同版同状態は timestamp も保存したまま返す。"""

        require_project_version(project.row_version, expected_row_version)
        if project.status != status.value:
            next_version = next_project_version(project.row_version)
            project.status = status.value
            project.row_version = next_version
            project.updated_at = now
        return self._to_stored(project)

    async def delete(self, *, project: Project, expected_row_version: int) -> None:
        """実行参照・所属監査・原 upload を持たない ARCHIVED Project と設定を削除する。

        key の一意制約は status を区別しないため、archive しただけでは key を再利用できない。
        ここは「作成し直したい」用途のための唯一の解放手段であり、Run または所属監査が
        一件でもあれば削除しない。未発火/認領中の Schedule と原 upload の占用も残す。
        設定削除は全ての検査後に同一 transaction で行い、FK RESTRICT を最後の防壁に保つ。
        """

        # Caller は Org→User→Session→Project を保持する。CAS は全参照判定より先に置く。
        require_project_version(project.row_version, expected_row_version)
        project_id, organization_id = project.id, project.organization_id
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
        has_member_audit = await self._session.scalar(
            select(exists().where(ProjectMemberEvent.project_id == project_id))
        )
        if has_member_audit:
            raise ProjectDeleteBlockedError(
                f"Project still has membership audit history: {project_id}",
                blockers=("member_audit_exists",),
            )
        # 新旧目録と清理/原 upload の監査は設定ではない。どれか一件でもあれば保全する。
        for document_model in (ProjectDocumentUpload, ProjectDocumentCleanup, ProjectDocument):
            if await self._session.scalar(select(exists().where(
                document_model.project_id == project_id,
            ))):
                raise ProjectDeleteBlockedError(
                    "Project still has document assets or upload/cleanup records",
                    blockers=("document_upload_exists",),
                )
        # 同じ gate の User だけを更新する。組織外の壊れた旧参照は FK RESTRICT で拒否する。
        await self._session.execute(
            update(User)
            .where(User.organization_id == organization_id, User.preferred_project_id == project_id)
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
                .where(
                    ProjectMember.project_id == project_id,
                    User.organization_id == organization_id,
                )
                .order_by(User.display_name, User.id)
            )
        ).all()
        return tuple(self._to_stored_member(member, user) for member, user in rows)

    async def lock_member(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        user_id: UUID,
        active_project: bool,
    ) -> ProjectMember | None:
        """Org/User/Session lock 後に Project→Member を取得し、判断と変更を分離する。"""

        project = await self._organization_project(
            organization_id=organization_id,
            project_id=project_id,
            lock=True,
        )
        if active_project and project.status != ProjectStatus.ACTIVE.value:
            raise ProjectNotFoundError(f"Active Project not found: {project_id}")
        member: ProjectMember | None = await self._session.scalar(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
            .with_for_update()
        )
        return member

    async def add_member(
        self,
        *,
        project_id: UUID,
        user: User,
        member: ProjectMember | None,
        actor_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> StoredProjectMember:
        """取得済み row の実変更だけを監査し、反復 ACTIVE add は何も書き換えない。"""

        if member is not None and member.status == ProjectMemberStatus.ACTIVE.value:
            return self._to_stored_member(member, user)
        previous_status = member.status if member is not None else None
        previous_joined_at = member.joined_at if member is not None else None
        if member is None:
            member = ProjectMember(
                id=uuid4(),
                project_id=project_id,
                user_id=user.id,
                status=ProjectMemberStatus.ACTIVE.value,
                joined_at=now,
                created_at=now,
                updated_at=now,
            )
            self._session.add(member)
            # relationship に依存せず、監査 FK の親を同じ transaction で先に INSERT する。
            await self._session.flush()
        else:
            member.status = ProjectMemberStatus.ACTIVE.value
            member.joined_at = now
            member.updated_at = now
        self.append_member_event(
            member=member, organization_id=user.organization_id, actor_id=actor_id,
            request_id=request_id, action=ProjectMemberAction.ADDED,
            previous_status=previous_status, previous_joined_at=previous_joined_at, now=now,
        )
        return self._to_stored_member(member, user)

    def remove_member(
        self,
        *,
        organization_id: UUID,
        member: ProjectMember,
        actor_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> None:
        """Membership の物理 row を残し、解除前後を同じ transaction に追加する。"""

        previous_status = member.status
        member.status = ProjectMemberStatus.REMOVED.value
        member.updated_at = now
        self.append_member_event(
            member=member, organization_id=organization_id, actor_id=actor_id,
            request_id=request_id, action=ProjectMemberAction.REMOVED,
            previous_status=previous_status, previous_joined_at=member.joined_at, now=now,
        )

    def append_member_event(
        self,
        *,
        member: ProjectMember,
        organization_id: UUID,
        actor_id: UUID,
        request_id: UUID,
        action: ProjectMemberAction,
        previous_status: str | None,
        previous_joined_at: datetime | None,
        now: datetime,
    ) -> None:
        """自由 payload や credential を受けず、変更前後の許可列だけを監査へ保存する。"""

        if not isinstance(request_id, UUID):
            raise ValueError("Membership audit requires a server request UUID")
        self._session.add(ProjectMemberEvent(
            id=uuid4(), organization_id=organization_id, project_id=member.project_id,
            member_id=member.id, user_id=member.user_id, actor_id=actor_id,
            action=action.value, previous_status=previous_status,
            previous_joined_at=previous_joined_at, status=member.status,
            joined_at=member.joined_at, request_id=request_id, created_at=now,
        ))

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
            row_version=project.row_version,
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
