"""SkillComposition(module)と Project 有効化の永続化を実装する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from projectmind.compositions.domain import (
    CreateModuleCommand,
    ModuleNotFoundError,
    ModuleSkillBinding,
    ModuleSkillInvalidError,
    StoredModule,
    UpdateModuleCommand,
)
from projectmind.db.models import (
    Project,
    ProjectComposition,
    ProjectSkillVersion,
    Skill,
    SkillComposition,
    SkillCompositionItem,
    SkillVersion,
)

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
            .where(
                ProjectComposition.project_id == project_id,
                SkillComposition.status == "ACTIVE",
                SkillComposition.presentation == MODULE_PRESENTATION,
            )
            .options(selectinload(SkillComposition.items))
            .order_by(SkillComposition.name)
        )
        compositions = (await self._session.scalars(statement)).all()
        return [
            await self._to_stored(project_id, composition) for composition in compositions
        ]

    async def create(self, command: CreateModuleCommand) -> StoredModule:
        """束縛検証の上で module を作成し、現在 Project へ有効化する。"""

        project = await self._session.get(Project, command.project_id)
        if project is None:
            raise ModuleNotFoundError(f"Project not found: {command.project_id}")
        await self._require_published_versions(command.project_id, command.skill_version_ids)
        now = datetime.now(UTC)
        composition = SkillComposition(
            id=uuid4(),
            organization_id=project.organization_id,
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
        self._session.add(
            ProjectComposition(
                id=uuid4(),
                project_id=command.project_id,
                composition_id=composition.id,
                enabled_by=command.created_by,
                created_at=now,
            )
        )
        return await self._to_stored(command.project_id, composition)

    async def update(self, command: UpdateModuleCommand) -> StoredModule:
        """名称/説明/束縛集合を置き換える。順序は指定順で振り直す。"""

        composition = await self._require_module(command.project_id, command.module_id)
        await self._require_published_versions(command.project_id, command.skill_version_ids)
        now = datetime.now(UTC)
        composition.name = command.name
        composition.description = command.description
        composition.updated_at = now
        # 同一 version の再束縛が uq に衝突しないよう、旧 item の削除を先に確定してから挿入する。
        composition.items = []
        await self._session.flush()
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
        return await self._to_stored(command.project_id, composition)

    async def delete(self, *, project_id: UUID, module_id: UUID) -> None:
        """Project の有効化を外し、他 Project が使っていなければ組合本体も削除する。"""

        composition = await self._require_module(project_id, module_id)
        enablements = (
            await self._session.scalars(
                select(ProjectComposition).where(
                    ProjectComposition.composition_id == composition.id
                )
            )
        ).all()
        for enablement in enablements:
            if enablement.project_id == project_id:
                await self._session.delete(enablement)
        if all(enablement.project_id == project_id for enablement in enablements):
            # 有効化行の削除を先に確定してから本体を消し、DB cascade との二重削除を避ける。
            await self._session.flush()
            await self._session.delete(composition)

    async def _require_module(
        self, project_id: UUID, module_id: UUID
    ) -> SkillComposition:
        """Project で有効な module を取得する。越権と不存在は同じ error へ畳む。"""

        statement = (
            select(SkillComposition)
            .join(
                ProjectComposition,
                ProjectComposition.composition_id == SkillComposition.id,
            )
            .where(
                SkillComposition.id == module_id,
                ProjectComposition.project_id == project_id,
                SkillComposition.presentation == MODULE_PRESENTATION,
            )
            .options(selectinload(SkillComposition.items))
        )
        composition = (await self._session.scalars(statement)).one_or_none()
        if composition is None:
            raise ModuleNotFoundError(f"Module not found: {module_id}")
        return composition

    async def _require_published_versions(
        self, project_id: UUID, skill_version_ids: tuple[UUID, ...]
    ) -> None:
        """束縛対象が全て Project 内の PUBLISHED SkillVersion であることを検証する。

        拒否理由は「未発行」「Project へ未有効化」「Project で無効化済み」で対処が全く異なる
        (docs/11 §6.0)。単一 join の有無だけでは区別できないため、status と有効化行を
        outer join で取得し、version ごとに理由を判定して返す。
        """

        if not skill_version_ids:
            return
        statement = (
            select(
                SkillVersion.id,
                SkillVersion.status,
                ProjectSkillVersion.id,
                ProjectSkillVersion.disabled_at,
            )
            .outerjoin(
                ProjectSkillVersion,
                (ProjectSkillVersion.skill_version_id == SkillVersion.id)
                & (ProjectSkillVersion.project_id == project_id),
            )
            .where(SkillVersion.id.in_(skill_version_ids))
        )
        rows = {
            row[0]: (row[1], row[2], row[3])
            for row in (await self._session.execute(statement)).all()
        }
        reasons = [
            f"{version_id} ({_binding_rejection(rows.get(version_id))})"
            for version_id in skill_version_ids
            if not _is_bindable(rows.get(version_id))
        ]
        if reasons:
            raise ModuleSkillInvalidError(
                f"Skill versions cannot be bound in this project: {'; '.join(reasons)}"
            )

    async def _to_stored(
        self, project_id: UUID, composition: SkillComposition
    ) -> StoredModule:
        """ORM 行を表示用 read model へ変換する。束縛には Skill 名と version を同梱する。"""

        version_ids = [item.skill_version_id for item in composition.items]
        names: dict[UUID, tuple[UUID, str, str, str]] = {}
        if version_ids:
            statement = (
                select(SkillVersion.id, Skill.id, Skill.key, Skill.name, SkillVersion.version)
                .join(Skill, SkillVersion.skill_id == Skill.id)
                .where(SkillVersion.id.in_(version_ids))
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


def _is_bindable(row: tuple[str, UUID | None, datetime | None] | None) -> bool:
    """SkillVersion が当該 Project の module へ束縛可能かを判定する。"""

    if row is None:
        return False
    status, enablement_id, disabled_at = row
    return status == "PUBLISHED" and enablement_id is not None and disabled_at is None


def _binding_rejection(row: tuple[str, UUID | None, datetime | None] | None) -> str:
    """束縛できない理由を、利用者の次の操作が判る語彙で返す。

    「未発行」は発行操作、「未有効化」は Skill library での Project 有効化、「無効化済み」は
    有効化の再開が必要であり、対処が異なる。同じ文言へ畳むと利用者がどこを直すか判らない。
    """

    if row is None:
        return "skill version not found in this organization"
    status, enablement_id, disabled_at = row
    if status != "PUBLISHED":
        return f"not PUBLISHED (status: {status})"
    if enablement_id is None:
        return "not enabled for this project"
    if disabled_at is not None:
        return "disabled for this project"
    return "not bindable"
