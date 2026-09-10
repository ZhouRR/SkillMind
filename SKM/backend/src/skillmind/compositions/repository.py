"""SkillComposition(module)と Project 有効化の永続化を実装する。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skillmind.compositions.domain import (
    CreateModuleCommand,
    ModuleNotFoundError,
    ModuleSkillBinding,
    ModuleSkillInvalidError,
    StoredModule,
    UpdateModuleCommand,
)
from skillmind.db.models import (
    Project,
    ProjectComposition,
    Skill,
    SkillComposition,
    SkillCompositionItem,
    SkillSource,
    SkillVersion,
)
from skillmind.skills.domain import PublishedTaskNotFoundError
from skillmind.skills.repository import SkillRepository

# 本 slice の UI は module のみ扱う。role/task_group は同じ table を将来使う。
MODULE_PRESENTATION = "module"


class CompositionRepository:
    """Transaction-scoped session 上で module と束縛を読み書きする。"""

    def __init__(self, session: AsyncSession) -> None:
        """Transaction-scoped database session を保持する。"""

        self._session = session

    async def list_for_project(self, project_id: UUID) -> list[StoredModule]:
        """Project で有効な ACTIVE module を名称昇順で列挙する。"""

        statement = (
            select(SkillComposition)
            .join(
                ProjectComposition,
                ProjectComposition.composition_id == SkillComposition.id,
            )
            .join(Project, Project.id == ProjectComposition.project_id)
            .where(
                ProjectComposition.project_id == project_id,
                SkillComposition.organization_id == Project.organization_id,
                SkillComposition.status == "ACTIVE",
                SkillComposition.presentation == MODULE_PRESENTATION,
            )
            .options(selectinload(SkillComposition.items))
            .order_by(SkillComposition.name)
        )
        compositions = (await self._session.scalars(statement)).all()
        return [await self._to_stored(project_id, composition) for composition in compositions]

    async def create(
        self,
        command: CreateModuleCommand,
        *,
        organization_id: UUID,
        authorize: Callable[[], datetime],
    ) -> StoredModule:
        """束縛検証の上で module を作成し、現在 Project へ有効化する。"""

        # 呼出元は同じ transaction で原 ADMIN と精確 ACTIVE Project を固定する。
        authorize()
        await self._require_published_versions(
            organization_id=organization_id,
            project_id=command.project_id,
            skill_version_ids=command.skill_version_ids,
            authorize=authorize,
        )
        now = authorize()
        composition = SkillComposition(
            id=uuid4(),
            organization_id=organization_id,
            name=command.name,
            description=command.description,
            presentation=MODULE_PRESENTATION,
            behavior_prompt="",
            config_json={},
            status="ACTIVE",
            created_at=now,
            updated_at=now,
            items=[
                SkillCompositionItem(
                    id=uuid4(),
                    skill_version_id=version_id,
                    sort_order=index,
                    enabled=True,
                    config_override_json={},
                    created_at=now,
                )
                for index, version_id in enumerate(command.skill_version_ids)
            ],
        )
        self._session.add(composition)
        # Relationship を張らない有効化行とは flush 順序が保証されないため、先に組合本体を確定する。
        await self._session.flush()
        authorize()
        self._session.add(
            ProjectComposition(
                id=uuid4(),
                project_id=command.project_id,
                composition_id=composition.id,
                enabled_by=command.created_by,
                created_at=now,
            )
        )
        stored = await self._to_stored(command.project_id, composition)
        authorize()
        return stored

    async def update(
        self,
        command: UpdateModuleCommand,
        *,
        organization_id: UUID,
        authorize: Callable[[], datetime],
    ) -> StoredModule:
        """名称/説明/束縛集合を置き換える。順序は指定順で振り直す。"""

        composition, _ = await self._require_module(
            organization_id=organization_id,
            project_id=command.project_id,
            module_id=command.module_id,
            authorize=authorize,
        )
        await self._require_published_versions(
            organization_id=organization_id,
            project_id=command.project_id,
            skill_version_ids=command.skill_version_ids,
            authorize=authorize,
        )
        now = authorize()
        composition.name = command.name
        composition.description = command.description
        composition.updated_at = now
        # 同一 version の再束縛が uq に衝突しないよう、旧 item の削除を先に確定してから挿入する。
        composition.items = []
        await self._session.flush()
        authorize()
        composition.items = [
            SkillCompositionItem(
                id=uuid4(),
                skill_version_id=version_id,
                sort_order=index,
                enabled=True,
                config_override_json={},
                created_at=now,
            )
            for index, version_id in enumerate(command.skill_version_ids)
        ]
        stored = await self._to_stored(command.project_id, composition)
        authorize()
        return stored

    async def delete(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        module_id: UUID,
        authorize: Callable[[], datetime],
    ) -> None:
        """Project の有効化を外し、他 Project が使っていなければ組合本体も削除する。"""

        composition, enablements = await self._require_module(
            organization_id=organization_id,
            project_id=project_id,
            module_id=module_id,
            authorize=authorize,
        )
        authorize()
        for enablement in enablements:
            if enablement.project_id == project_id:
                await self._session.delete(enablement)
                authorize()
        if all(enablement.project_id == project_id for enablement in enablements):
            # 有効化行の削除を先に確定してから本体を消し、DB cascade との二重削除を避ける。
            await self._session.flush()
            authorize()
            await self._session.delete(composition)
            authorize()

    async def _require_module(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        module_id: UUID,
        authorize: Callable[[], datetime],
    ) -> tuple[SkillComposition, tuple[ProjectComposition, ...]]:
        """Project で有効な module を取得する。越権と不存在は同じ error へ畳む。"""

        authorize()
        statement = (
            select(SkillComposition)
            .join(
                ProjectComposition,
                ProjectComposition.composition_id == SkillComposition.id,
            )
            .join(Project, Project.id == ProjectComposition.project_id)
            .where(
                SkillComposition.id == module_id,
                SkillComposition.organization_id == organization_id,
                Project.organization_id == organization_id,
                ProjectComposition.project_id == project_id,
                SkillComposition.presentation == MODULE_PRESENTATION,
            )
            .options(selectinload(SkillComposition.items))
            .with_for_update(of=SkillComposition)
            .execution_options(populate_existing=True)
        )
        composition = (await self._session.scalars(statement)).one_or_none()
        authorize()
        if composition is None:
            raise ModuleNotFoundError("Module not found in project")
        # 親→関連 ID 順を全変更で揃える。共有先を削除せず、最終関連かどうかを鎖内で判定する。
        enablements = tuple(
            (
                await self._session.scalars(
                    select(ProjectComposition)
                    .where(ProjectComposition.composition_id == composition.id)
                    .order_by(ProjectComposition.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        authorize()
        if not any(item.project_id == project_id for item in enablements):
            raise ModuleNotFoundError("Module not found in project")
        return composition, enablements

    async def _require_published_versions(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        skill_version_ids: tuple[UUID, ...],
        authorize: Callable[[], datetime],
    ) -> None:
        """共通の精確版 gate を ID 順で取り、元の表示順とは分離する。"""

        repository = SkillRepository(self._session)
        for version_id in sorted(set(skill_version_ids)):
            try:
                await repository.require_current_task_binding(
                    organization_id=organization_id,
                    project_id=project_id,
                    skill_version_id=version_id,
                    authorize=authorize,
                )
            except PublishedTaskNotFoundError as error:
                authorize()
                # 不存在と他組織・非公開・停止を同型にし、他組織の状態を漏らさない。
                raise ModuleSkillInvalidError(
                    "Skill versions are not available for this project"
                ) from error

    async def _to_stored(self, project_id: UUID, composition: SkillComposition) -> StoredModule:
        """ORM 行を表示用 read model へ変換する。束縛には Skill 名と version を同梱する。"""

        version_ids = [item.skill_version_id for item in composition.items]
        names: dict[UUID, tuple[UUID, str, str, str]] = {}
        if version_ids:
            statement = (
                select(SkillVersion.id, Skill.id, Skill.key, Skill.name, SkillVersion.version)
                .join(Skill, SkillVersion.skill_id == Skill.id)
                .join(SkillSource, SkillSource.id == SkillVersion.skill_source_id)
                .where(
                    SkillVersion.id.in_(version_ids),
                    Skill.organization_id == composition.organization_id,
                    SkillSource.organization_id == composition.organization_id,
                )
            )
            for version_id, skill_id, skill_key, skill_name, version in (
                await self._session.execute(statement)
            ).all():
                names[version_id] = (skill_id, skill_key, skill_name, version)
        skills = []
        for item in sorted(composition.items, key=lambda entry: entry.sort_order):
            skill_id, skill_key, skill_name, version = names.get(
                item.skill_version_id, (item.skill_version_id, "unknown", "Unknown", "-")
            )
            skills.append(
                ModuleSkillBinding(
                    skill_version_id=item.skill_version_id,
                    skill_id=skill_id,
                    skill_key=skill_key,
                    skill_name=skill_name,
                    version=version,
                    sort_order=item.sort_order,
                )
            )
        return StoredModule(
            module_id=composition.id,
            project_id=project_id,
            name=composition.name,
            description=composition.description,
            skills=tuple(skills),
            created_at=composition.created_at,
            updated_at=composition.updated_at,
        )
