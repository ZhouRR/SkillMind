"""Module(SkillComposition)use case の transaction 境界を実装する。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.compositions.domain import (
    CreateModuleCommand,
    ModuleNotFoundError,
    ModuleSkillInvalidError,
    ModuleValidationError,
    StoredModule,
    UpdateModuleCommand,
)
from projectmind.compositions.repository import CompositionRepository
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from projectmind.projects.repository import LockedProjectAccess, ProjectRepository
from projectmind.users.access import authorize_user_access, validate_user_access
from projectmind.users.domain import UserAccess
from projectmind.users.repository import LockedUsers, UserRepository, authorization_failure_snapshot

MODULE_NAME_MAX_LENGTH = 200
MODULE_DESCRIPTION_MAX_LENGTH = 2000
MODULE_MAX_SKILLS = 50


class CompositionService:
    """Module の一覧・作成・更新・削除 use case を所有する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Database session factory を保持する。"""

        self._session_factory = session_factory

    async def list_modules(self, *, project_id: UUID) -> list[StoredModule]:
        """Project で有効な module を列挙する。"""

        async with self._session_factory() as session:
            return await CompositionRepository(session).list_for_project(project_id)

    async def create_module(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        name: str,
        description: str,
        skill_version_ids: list[UUID],
    ) -> StoredModule:
        """入力を正規化・検証して module を作成し、現在 Project へ有効化する。"""

        access = deepcopy(access)
        versions = _normalized_versions(skill_version_ids)
        name = _normalized_name(name)
        description = _normalized_description(description)
        async with self._write_transaction(access, project_id) as (repository, locked, authorize):
            return await repository.create(
                CreateModuleCommand(
                    project_id=project_id,
                    created_by=locked.actor.id,
                    name=name,
                    description=description,
                    skill_version_ids=versions,
                ),
                organization_id=locked.actor.organization_id,
                authorize=authorize,
            )

    async def update_module(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        module_id: UUID,
        name: str,
        description: str,
        skill_version_ids: list[UUID],
    ) -> StoredModule:
        """名称/説明/束縛集合を置き換える。"""

        access = deepcopy(access)
        command = UpdateModuleCommand(
            project_id=project_id,
            module_id=module_id,
            name=_normalized_name(name),
            description=_normalized_description(description),
            skill_version_ids=_normalized_versions(skill_version_ids),
        )
        async with self._write_transaction(access, project_id) as (repository, locked, authorize):
            return await repository.update(
                command,
                organization_id=locked.actor.organization_id,
                authorize=authorize,
            )

    async def delete_module(self, *, access: UserAccess, project_id: UUID, module_id: UUID) -> None:
        """現在の ADMIN 資格で Project の関連を外し、最終関連なら組合も削除する。"""

        access = deepcopy(access)
        async with self._write_transaction(access, project_id) as (repository, locked, authorize):
            await repository.delete(
                organization_id=locked.actor.organization_id,
                project_id=project_id,
                module_id=module_id,
                authorize=authorize,
            )

    @asynccontextmanager
    async def _write_transaction(
        self, access: UserAccess, project_id: UUID
    ) -> AsyncIterator[tuple[CompositionRepository, LockedUsers, Callable[[], datetime]]]:
        """原会話と ACTIVE Project を固定し、全組合変更を最終資格と同じ transaction に置く。"""

        validate_user_access(access)
        async with self._session_factory() as session, session.begin():
            locked = await UserRepository(session).lock_users(
                access=access,
                target_id=None,
                include_target_sessions=False,
                read_only_actor=True,
            )
            project: LockedProjectAccess | None = None

            def authorize() -> datetime:
                """待機で古くなった入口 actor ではなく、原資格と現在時刻で再判定する。"""

                now = authorize_user_access(
                    access, locked, now=datetime.now(UTC), admin=True, write=True
                )
                if project is not None:
                    ProjectRepository.require_active_write_access(project)
                return now

            authorize()
            failure_snapshot = authorization_failure_snapshot(locked)
            try:
                project = await ProjectRepository(session).lock_write_access(
                    user=locked.actor, project_id=project_id
                )
                authorize()
                yield CompositionRepository(session), locked, authorize
                authorize()
                await session.flush()
                authorize()
            except (
                ProjectNotFoundError,
                ProjectArchivedError,
                ModuleNotFoundError,
                ModuleSkillInvalidError,
                ModuleValidationError,
            ):
                # 対象情報を返す前にも期限を確認し、失効した要求へ遅い業務理由を漏らさない。
                authorize()
                raise
            except SQLAlchemyError:
                # SQL 失敗後は expire 済み ORM を読まず、鎖内の値で失敗分類だけを行う。
                authorize_user_access(
                    access, failure_snapshot, now=datetime.now(UTC), admin=True, write=True
                )
                raise


def _normalized_name(name: str) -> str:
    """名称を trim し、空と超過を拒否する。"""

    normalized = name.strip()
    if not normalized:
        raise ModuleValidationError("Module name must not be empty")
    if len(normalized) > MODULE_NAME_MAX_LENGTH:
        raise ModuleValidationError("Module name is too long")
    return normalized


def _normalized_description(description: str) -> str:
    """説明を trim し、超過だけ拒否する(空は許可)。"""

    normalized = description.strip()
    if len(normalized) > MODULE_DESCRIPTION_MAX_LENGTH:
        raise ModuleValidationError("Module description is too long")
    return normalized


def _normalized_versions(skill_version_ids: list[UUID]) -> tuple[UUID, ...]:
    """束縛集合の重複を宣言順で除去し、空と超過を拒否する。"""

    unique: list[UUID] = []
    for version_id in skill_version_ids:
        if version_id not in unique:
            unique.append(version_id)
    if not unique:
        raise ModuleValidationError("Module must bind at least one published skill")
    if len(unique) > MODULE_MAX_SKILLS:
        raise ModuleValidationError("Module binds too many skills")
    return tuple(unique)
