"""実認証 repository と管理 service を、明示 SQL と局部 transaction fake で結ぶ。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from sqlalchemy import Select, and_

from projectmind.auth.domain import generate_session_credentials
from projectmind.auth.service import AuthenticatedActor
from projectmind.db.models import (
    AuthSession,
    Organization,
    Project,
    ProjectMember,
    ProjectSkillVersion,
    SkillVersion,
    User,
)
from projectmind.schedules.domain import (
    ScheduleDefinition,
    ScheduleKind,
    ScheduleRecord,
    ScheduleStatus,
)
from projectmind.schedules.repository import ScheduleRepository
from projectmind.schedules.service import ScheduleService
from projectmind.users.domain import UserAccess
from tests.runs.task_binding_fakes import TaskBindingRows
from tests.schedules.fakes import ScheduleDatabase


class ScheduleAuthorizationDatabase(ScheduleDatabase):
    """認証を bypass せず、DB 待機と commit 応答だけを合成する。"""

    def __init__(self, *, claimed: bool = False) -> None:
        """同一組織の原会話・Project 所属と、実 service の依存を準備する。"""

        super().__init__(claimed=claimed)
        now = datetime.now(UTC)
        self.organization: Organization | None = Organization(id=uuid4(), name="Synthetic org")
        self.user = User(
            id=self.schedule.created_by,
            organization_id=self.organization.id,
            email="schedule-reader@example.test",
            display_name="Schedule reader",
            password_hash="unused",
            system_role="USER",
            status="ACTIVE",
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self.users = [self.user]
        credentials = generate_session_credentials()
        self.auth_session = AuthSession(
            id=uuid4(),
            user_id=self.user.id,
            token_hash=credentials.session_token_hash,
            csrf_token_hash=credentials.csrf_token_hash,
            credential_version=2,
            system_role_at_login="USER",
            revoked_at=None,
            created_at=now,
            last_seen_at=now,
            idle_expires_at=now + timedelta(minutes=30),
            absolute_expires_at=now + timedelta(hours=8),
        )
        self.auth_sessions = [self.auth_session]
        self.access = UserAccess(
            actor=AuthenticatedActor(
                self.user.id,
                self.user.organization_id,
                self.user.email,
                self.user.display_name,
                "USER",
            ),
            request_id=uuid4(),
            session_token=credentials.session_token,
            csrf_token=credentials.csrf_token,
        )
        self.project = Project(
            id=self.schedule.project_id,
            organization_id=self.user.organization_id,
            key="schedule-synthetic",
            name="Schedule synthetic",
            description="",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self.member: ProjectMember | None = ProjectMember(
            id=uuid4(),
            project_id=self.project.id,
            user_id=self.user.id,
            status="ACTIVE",
            joined_at=now,
            created_at=now,
            updated_at=now,
        )
        self.project_present = True
        self.task_binding = TaskBindingRows(
            self.project, SkillVersion(id=self.schedule.skill_version_id, status="PUBLISHED")
        )
        self.on_lock: Callable[[type[Any]], None] | None = None
        self.on_flush: Callable[[], None] | None = None
        self.on_resolve: Callable[[], None] | None = None
        self.lock_events: list[type[Any]] = []
        self.transactions = 0
        self.commits = 0
        self.rollbacks = 0
        self.in_transaction = False
        self.commit_error = False
        self.commit_persists = False
        self.commit_entered = asyncio.Event()
        self.commit_release: asyncio.Event | None = None
        self.session.__aenter__.return_value = self.session
        self.session.begin.side_effect = self.transaction
        self.session.flush.side_effect = self.flush
        self.skills = MagicMock()
        self.skills.resolve_task_run = AsyncMock(side_effect=self.resolve_task)
        self.runs_service = MagicMock()
        self.runs_service.validate_task_sources = AsyncMock(side_effect=self.validate_sources)
        self.session_factory = MagicMock(return_value=self.session)
        self.service = ScheduleService(
            self.session_factory, skill_service=self.skills, run_service=self.runs_service
        )

    def observe_lock(self, statement: Select[Any]) -> None:
        """待機後 callback を明示的な lock query だけに配送する。"""

        if statement._for_update_arg is not None:
            entity = statement.column_descriptions[0]["entity"]
            self.lock_events.append(entity)
            if self.on_lock:
                self.on_lock(entity)

    async def scalar(self, statement: Select[Any]) -> Any:
        """Project/所属/組織は原 SQL の scope を評価し、未知 SQL は親 fake でも拒否する。"""

        self.observe_lock(statement)
        entity = statement.column_descriptions[0]["entity"]
        if entity in (SkillVersion, ProjectSkillVersion):
            self.statements.append(statement)
            return self.task_binding.scalar(statement)
        if entity not in (Organization, Project, ProjectMember):
            return await super().scalar(statement)
        assert statement.column_descriptions[0]["expr"] is entity
        self.statements.append(statement)
        params = statement.compile().params
        if entity is Organization:
            assert set(params) == {"id_1"}
            assert statement.whereclause is not None and statement.whereclause.compare(
                Organization.id == params["id_1"]
            )
            return (
                self.organization
                if (self.organization is not None and self.organization.id == params["id_1"])
                else None
            )
        if entity is Project:
            assert set(params) == {"id_1", "organization_id_1"}
            assert statement.whereclause is not None and statement.whereclause.compare(
                and_(
                    Project.id == params["id_1"],
                    Project.organization_id == params["organization_id_1"],
                )
            )
            return (
                self.project
                if (
                    self.project_present
                    and self.project.id == params["id_1"]
                    and self.project.organization_id == params["organization_id_1"]
                )
                else None
            )
        assert set(params) == {"project_id_1", "user_id_1"}
        assert statement.whereclause is not None and statement.whereclause.compare(
            and_(
                ProjectMember.project_id == params["project_id_1"],
                ProjectMember.user_id == params["user_id_1"],
            )
        )
        return (
            self.member
            if (
                self.member is not None
                and self.member.project_id == params["project_id_1"]
                and self.member.user_id == params["user_id_1"]
            )
            else None
        )

    async def scalars(self, statement: Select[Any]) -> Any:
        """本物 UserRepository の原 User/Session 条件を省略せず処理する。"""

        self.observe_lock(statement)
        entity = statement.column_descriptions[0]["entity"]
        if entity not in (User, AuthSession):
            return await super().scalars(statement)
        assert statement.column_descriptions[0]["expr"] is entity
        self.statements.append(statement)
        params = statement.compile().params
        rows: list[Any]
        if entity is User:
            assert set(params) == {"organization_id_1", "id_1"}
            assert statement.whereclause is not None and statement.whereclause.compare(
                and_(
                    User.organization_id == params["organization_id_1"],
                    User.id.in_(params["id_1"]),
                )
            )
            rows = [
                user
                for user in self.users
                if (
                    user.organization_id == params["organization_id_1"]
                    and user.id in params["id_1"]
                )
            ]
        else:
            assert set(params) == {"user_id_1", "token_hash_1"}
            assert statement.whereclause is not None and statement.whereclause.compare(
                and_(
                    AuthSession.user_id == params["user_id_1"],
                    AuthSession.token_hash == params["token_hash_1"],
                )
            )
            rows = [
                session
                for session in self.auth_sessions
                if (
                    session.user_id == params["user_id_1"]
                    and session.token_hash == params["token_hash_1"]
                )
            ]
        result = MagicMock()
        result.all.return_value = rows
        return result

    async def resolve_task(self, **arguments: Any) -> object:
        """Skill 解決中に DB lock を保持していないことを検証する。"""

        del arguments
        assert not self.in_transaction
        if self.on_resolve:
            self.on_resolve()
        return object()

    async def validate_sources(self, **arguments: Any) -> None:
        """資源選択検証も管理 transaction 外の既存依存へ渡す。"""

        del arguments
        assert not self.in_transaction

    async def flush(self) -> None:
        """最後の FK/制約待機を含む時刻・失効の変化を注入する。"""

        if self.on_flush:
            self.on_flush()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[ScheduleRepository]:
        """業務行全体を復元し、commit 応答喪失の持続/非持続を明確に区別する。"""

        self.transactions += 1
        self.in_transaction = True
        try:
            async with super().transaction():
                try:
                    yield self.repo
                    self.commit_entered.set()
                    if self.commit_release is not None:
                        await self.commit_release.wait()
                    if self.commit_error and not self.commit_persists:
                        raise ConnectionError("Synthetic commit acknowledgement failure")
                except BaseException:
                    self.rollbacks += 1
                    raise
            self.commits += 1
            if self.commit_error:
                raise ConnectionError("Synthetic commit acknowledgement failure")
        finally:
            self.in_transaction = False

    async def submit(self, operation: str) -> ScheduleRecord:
        """三つの公開管理 use case に必須の原 UserAccess を必ず渡す。"""

        definition = ScheduleDefinition(ScheduleKind.CRON, "UTC", "*/5 * * * *", None, None, 5)
        if operation == "create":
            return await self.service.create_schedule(
                project_id=self.project.id,
                access=self.access,
                name="New schedule",
                definition=definition,
                skill_version_id=self.schedule.skill_version_id,
                task_key=self.schedule.task_key,
                input_json={"value": 2},
                sources={},
            )
        if operation == "edit":
            return await self.service.update_schedule(
                project_id=self.project.id,
                schedule_id=self.schedule.id,
                access=self.access,
                name="Changed schedule",
                definition=definition,
                input_json={"value": 2},
                sources={},
                expected_row_version=self.schedule.row_version,
            )
        if operation == "status":
            return await self.service.change_status(
                project_id=self.project.id,
                schedule_id=self.schedule.id,
                access=self.access,
                target=ScheduleStatus.PAUSED,
                expected_row_version=self.schedule.row_version,
            )
        raise AssertionError(f"Unknown management operation: {operation}")
