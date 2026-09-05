"""Module(SkillComposition)use case の transaction 境界を実装する。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.compositions.domain import (
    CreateModuleCommand,
    ModuleValidationError,
    StoredModule,
    UpdateModuleCommand,
)
from projectmind.compositions.repository import CompositionRepository

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
        project_id: UUID,
        created_by: UUID,
        name: str,
        description: str,
        skill_version_ids: list[UUID],
    ) -> StoredModule:
        """入力を正規化・検証して module を作成し、現在 Project へ有効化する。"""

        command = CreateModuleCommand(
            project_id=project_id,
            created_by=created_by,
            name=_normalized_name(name),
            description=_normalized_description(description),
            skill_version_ids=_normalized_versions(skill_version_ids),
        )
        async with self._session_factory() as session, session.begin():
            return await CompositionRepository(session).create(command)

    async def update_module(
        self,
        *,
        project_id: UUID,
        module_id: UUID,
        name: str,
        description: str,
        skill_version_ids: list[UUID],
    ) -> StoredModule:
        """名称/説明/束縛集合を置き換える。"""

        command = UpdateModuleCommand(
            project_id=project_id,
            module_id=module_id,
            name=_normalized_name(name),
            description=_normalized_description(description),
            skill_version_ids=_normalized_versions(skill_version_ids),
        )
        async with self._session_factory() as session, session.begin():
            return await CompositionRepository(session).update(command)

    async def delete_module(self, *, project_id: UUID, module_id: UUID) -> None:
        """Project の module を削除する。"""

        async with self._session_factory() as session, session.begin():
            await CompositionRepository(session).delete(
                project_id=project_id, module_id=module_id
            )


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
