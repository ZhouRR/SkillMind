"""実 Composition/資格 repository を局部 SQL 応答へ接続する。

保存集合と rollback は合成であり、PostgreSQL の並行 lock/物理 commit を証明しない。
外部の会話・Project・版の失効は、自分の組合 rollback では復活させない。
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self, cast
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from projectmind.compositions.domain import StoredModule
from projectmind.compositions.service import CompositionService
from projectmind.db.models import (
    AuthSession,
    Organization,
    Project,
    ProjectComposition,
    ProjectSkillVersion,
    Skill,
    SkillComposition,
    SkillCompositionItem,
    SkillSource,
    SkillVersion,
    User,
)
from projectmind.users.domain import UserAccess
from tests.skills.skill_lifecycle_harness import POSTGRESQL_DIALECT, LifecycleResult
from tests.skills.test_skill_publication_authorization import AuthorizationSession

Operation = Literal["create", "update", "delete"]
OPERATIONS: tuple[Operation, ...] = ("create", "update", "delete")
Asset = SkillComposition | SkillCompositionItem | ProjectComposition


def columns(row: Any) -> dict[str, Any]:
    """ORM の保存列だけを比較し、relationship の循環や内部 state を写さない。"""
    return deepcopy({column.key: getattr(row, column.key) for column in row.__table__.columns})


class CompositionTransaction:
    """組合だけの rollback と、commit が済んだか不明な二つの結末を合成する。"""

    def __init__(self, owner: CompositionSession) -> None:
        """現在資格の行は組合の復元対象へ含めない。"""
        self.owner = owner
        self.original: list[tuple[Asset, dict[str, Any]]] = []
        self.compositions: list[SkillComposition] = []
        self.relationships: dict[UUID, list[SkillCompositionItem]] = {}
        self.enablements: list[ProjectComposition] = []
        self.flushed: set[UUID] = set()
        self.target: SkillComposition | None = None
        self.module_id = owner.module_id

    async def __aenter__(self) -> Self:
        """初回 add と既存 relationship の置換を共に戻せるよう原集合を記録する。"""
        self.owner.transactions += 1
        self.owner.events.append("begin")
        self.target = self.owner.composition
        self.compositions = list(self.owner.compositions)
        self.enablements = list(self.owner.enablements)
        self.relationships = {row.id: list(row.items) for row in self.compositions}
        self.flushed = set(self.owner.flushed_compositions)
        self.original = [(row, columns(row)) for row in self.owner.assets()]
        return self

    def restore(self) -> None:
        """自分の変更前へ戻すだけで、別の資格失効や新しい actor を復活させない。"""
        self.owner.failure_values = self.owner.frozen_values()
        for row, values in self.original:
            for key, value in values.items():
                setattr(row, key, deepcopy(value))
        for row in self.compositions:
            row.items = self.relationships[row.id]
        self.owner.compositions[:] = self.compositions
        self.owner.enablements[:] = self.enablements
        self.owner.flushed_compositions = set(self.flushed)
        self.owner.composition = self.target
        self.owner.module_id = self.module_id
        self.owner.rollbacks += 1
        self.owner.events.append("rollback")

    async def __aexit__(self, kind: object, error: object, traceback: object) -> None:
        """commit 例外は原型のまま伝え、応答喪失を成功や未提交と決め付けない。"""
        if kind is not None:
            self.restore()
            return
        self.owner.emit("commit")
        if self.owner.commit_outcome == "not-committed":
            self.restore()
            raise self.owner.commit_error
        self.owner.commits += 1
        self.owner.events.append("commit")
        if self.owner.commit_outcome == "committed":
            raise self.owner.commit_error


class CompositionSession:
    """API からも注入できる合成 aggregate。業務判定を置き換える fake service ではない。"""

    def __init__(self, operation: Operation = "create") -> None:
        """原資格と三つの合法な精確版を準備し、入力順と lock 順を意図的に変える。"""
        self.authority = AuthorizationSession()
        self.operation = operation
        self.users = self.authority.users
        self.auth_sessions = self.authority.auth_sessions
        self.organization_id = self.authority.organization_id
        self.skill = self.authority.skill
        self.source = self.authority.source
        self.manifest = self.authority.manifest
        self.interpretation = self.authority.interpretation
        now = datetime.now(UTC)
        self.auth_session.idle_expires_at = now + timedelta(minutes=30)
        self.auth_session.absolute_expires_at = now + timedelta(hours=8)
        self.project = Project(
            id=uuid4(),
            organization_id=self.organization_id,
            key="composition-test",
            name="Synthetic composition project",
            description="",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self.project_present = True
        self.versions = [
            SkillVersion(
                **{
                    **columns(self.authority.version),
                    "id": UUID(f"00000000-0000-4000-8000-{index:012d}"),
                    "version": f"1.0.{index}",
                    "status": "PUBLISHED",
                }
            )
            for index in (1, 2, 3)
        ]
        self.version = self.versions[0]
        self.version_ids = [self.versions[index].id for index in (2, 0, 2, 1)]
        self.bindings = [
            ProjectSkillVersion(
                id=uuid4(),
                project_id=self.project.id,
                skill_version_id=version.id,
                enabled_by=self.user.id,
                enabled_at=now,
                disabled_at=None,
            )
            for version in self.versions
        ]
        self.composition: SkillComposition | None = SkillComposition(
            id=uuid4(),
            organization_id=self.organization_id,
            name="Original shared module",
            description="Original description",
            presentation="module",
            behavior_prompt="",
            config_json={},
            status="ACTIVE",
            created_at=now,
            updated_at=now,
            items=[],
        )
        self.module_id = self.composition.id
        self.composition.items = [
            SkillCompositionItem(
                id=uuid4(),
                composition_id=self.module_id,
                skill_version_id=self.version.id,
                sort_order=0,
                enabled=True,
                config_override_json={},
                created_at=now,
            )
        ]
        self.compositions = [] if operation == "create" else [self.composition]
        self.enablements = (
            []
            if operation == "create"
            else [
                ProjectComposition(
                    id=uuid4(),
                    project_id=self.project.id,
                    composition_id=self.module_id,
                    enabled_by=self.user.id,
                    created_at=now,
                )
            ]
        )
        self.flushed_compositions = {row.id for row in self.compositions}
        self.missing: set[type[Any]] = set()
        self.timeline: list[str] = []
        self.queries: list[str] = []
        self.events: list[str] = []
        self.visits: dict[str, int] = {}
        self.on_step: Callable[[str], None] | None = None
        self.locked_versions: list[UUID] = []
        self.mutations: list[str] = []
        self.transactions = self.commits = self.rollbacks = 0
        self.failure_values: dict[str, object] | None = None
        self.commit_outcome: str | None = None
        self.commit_error: Exception = ConnectionError("Synthetic composition commit unknown")

    @property
    def access(self) -> UserAccess:
        """共有持続資格の原要求値を返す。"""
        return self.authority.access

    @access.setter
    def access(self, value: UserAccess) -> None:
        """資格を変える反例でも、実 UserRepository が同じ要求値を受け取る。"""
        self.authority.access = value

    @property
    def user(self) -> User:
        """権限はこの現在 ORM 行を実 authorizer に判定させる。"""
        return self.authority.user

    @property
    def auth_session(self) -> AuthSession:
        """原 token に対応する保存行を直接変更できるよう公開する。"""
        return self.authority.auth_session

    @property
    def items(self) -> list[SkillCompositionItem]:
        """今ある原組合の item 集合だけを返す。"""
        return self.composition.items if self.composition is not None else []

    async def __aenter__(self) -> Self:
        """実 Service の session context を提供するが、実接続は作らない。"""
        return self

    async def __aexit__(self, *args: object) -> None:
        """session 終了を観測し、未終了 process と区別する。"""
        self.events.append("close")

    def begin(self) -> CompositionTransaction:
        """実 Service が所有する transaction の保存集合を追跡する。"""
        return CompositionTransaction(self)

    def emit(self, name: str) -> None:
        """各 SQL 応答/flush 後に一回だけ待機注入を行う。"""
        self.visits[name] = self.visits.get(name, 0) + 1
        point = f"{name}:{self.visits[name]}"
        self.timeline.append(point)
        if self.on_step is not None:
            self.on_step(point)

    def assets(self) -> list[Asset]:
        """この管理操作だけが変更する組合/子 item/有効化行を列挙する。"""
        return [
            *self.compositions,
            *self.enablements,
            *(item for row in self.compositions for item in row.items),
        ]

    def frozen_values(self) -> dict[str, object]:
        """全組合資産と元 Skill 内容を比較し、失効 ORM の資格行には触れない。"""
        return {
            "assets": [(type(row).__name__, columns(row)) for row in self.assets()],
            "versions": [columns(row) for row in self.versions],
            "manifest": columns(self.manifest),
            "source": columns(self.source),
        }

    def service(self) -> CompositionService:
        """四つの本番 repository と実 service をそのまま session factory へ接続する。"""
        return CompositionService(cast(async_sessionmaker[AsyncSession], lambda: self))

    async def operate(self) -> StoredModule | None:
        """同じ原 access と表示順で公開三用例を呼び、自動 retry は実装しない。"""
        service = self.service()
        if self.operation == "create":
            return await service.create_module(
                access=self.access,
                project_id=self.project.id,
                name=" Updated module ",
                description=" Updated description ",
                skill_version_ids=list(self.version_ids),
            )
        if self.operation == "update":
            return await service.update_module(
                access=self.access,
                project_id=self.project.id,
                module_id=self.module_id,
                name=" Updated module ",
                description=" Updated description ",
                skill_version_ids=list(self.version_ids),
            )
        await service.delete_module(
            access=self.access,
            project_id=self.project.id,
            module_id=self.module_id,
        )
        return None

    async def scalar(self, statement: Select[tuple[Any, ...]]) -> Any:
        """実資格/Project/精確版の SQL predicate と lock を局部データへ適用する。"""
        entity = statement.column_descriptions[0].get("entity")
        parameters = statement.compile().params
        if entity is Organization:
            organization_result = await self.authority.scalar(statement)
            self.emit("organization")
            return organization_result
        if entity is Project:
            expected = (
                select(Project)
                .where(
                    Project.id == parameters["id_1"],
                    Project.organization_id == parameters["organization_id_1"],
                )
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            assert statement.compare(expected)
            self.queries.append(str(statement.compile(dialect=POSTGRESQL_DIALECT)))
            result = (
                self.project
                if (
                    self.project_present
                    and self.project.id == parameters["id_1"]
                    and self.project.organization_id == parameters["organization_id_1"]
                )
                else None
            )
            self.emit("project")
            return result
        if entity is SkillVersion:
            expected_version = (
                select(SkillVersion)
                .join(
                    SkillSource,
                    SkillSource.id == SkillVersion.skill_source_id,
                )
                .join(Skill, Skill.id == SkillVersion.skill_id)
                .join(
                    Project,
                    Project.organization_id == Skill.organization_id,
                )
                .where(
                    SkillVersion.id == parameters["id_1"],
                    SkillVersion.status == parameters["status_1"],
                    Skill.organization_id == parameters["organization_id_1"],
                    SkillSource.organization_id == parameters["organization_id_2"],
                    Project.id == parameters["id_2"],
                )
                .with_for_update(read=True, of=SkillVersion)
                .execution_options(populate_existing=True)
            )
            assert statement.compare(expected_version)
            assert parameters["status_1"] == "PUBLISHED"
            version = next((row for row in self.versions if row.id == parameters["id_1"]), None)
            found = version is not None and (
                SkillVersion not in self.missing
                and Skill not in self.missing
                and SkillSource not in self.missing
                and self.project_present
                and version.skill_id == self.skill.id
                and version.skill_source_id == self.source.id
                and version.status == "PUBLISHED"
                and self.skill.organization_id == parameters["organization_id_1"]
                and self.source.organization_id == parameters["organization_id_2"]
                and self.project.id == parameters["id_2"]
                and self.project.organization_id == self.skill.organization_id
            )
            self.queries.append(str(statement.compile(dialect=POSTGRESQL_DIALECT)))
            self.locked_versions.append(parameters["id_1"])
            self.emit("version")
            return version if found else None
        if entity is ProjectSkillVersion:
            expected_binding = (
                select(ProjectSkillVersion)
                .where(
                    ProjectSkillVersion.project_id == parameters["project_id_1"],
                    ProjectSkillVersion.skill_version_id == parameters["skill_version_id_1"],
                )
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            assert statement.compare(expected_binding)
            matches = [
                row
                for row in self.bindings
                if row.project_id == parameters["project_id_1"]
                and row.skill_version_id == parameters["skill_version_id_1"]
            ]
            assert len(matches) <= 1
            self.queries.append(str(statement.compile(dialect=POSTGRESQL_DIALECT)))
            self.emit("binding")
            return matches[0] if matches else None
        raise AssertionError(f"Unexpected scalar model: {entity}")

    async def scalars(self, statement: Select[tuple[Any, ...]]) -> Any:
        """共有資格 SQL 以外は、確定した組合 SQL seam の実装が必須である。"""
        entity = statement.column_descriptions[0].get("entity")
        if entity in {User, AuthSession}:
            result = await self.authority.scalars(statement)
            self.emit(f"select-{entity.__name__}")
            return result
        parameters = statement.compile().params
        if entity is SkillComposition:
            expected = (
                select(SkillComposition)
                .join(
                    ProjectComposition,
                    ProjectComposition.composition_id == SkillComposition.id,
                )
                .join(Project, Project.id == ProjectComposition.project_id)
            )
            if "id_1" in parameters:
                expected = (
                    expected.where(
                        SkillComposition.id == parameters["id_1"],
                        SkillComposition.organization_id == parameters["organization_id_1"],
                        Project.organization_id == parameters["organization_id_2"],
                        ProjectComposition.project_id == parameters["project_id_1"],
                        SkillComposition.presentation == parameters["presentation_1"],
                    )
                    .options(selectinload(SkillComposition.items))
                    .with_for_update(
                        of=SkillComposition,
                    )
                    .execution_options(populate_existing=True)
                )
                matches = [
                    row
                    for row in self.compositions
                    if row.id == parameters["id_1"]
                    and row.organization_id == parameters["organization_id_1"]
                    and self.project.organization_id == parameters["organization_id_2"]
                ]
            else:
                expected = (
                    expected.where(
                        ProjectComposition.project_id == parameters["project_id_1"],
                        SkillComposition.organization_id == Project.organization_id,
                        SkillComposition.status == parameters["status_1"],
                        SkillComposition.presentation == parameters["presentation_1"],
                    )
                    .options(selectinload(SkillComposition.items))
                    .order_by(SkillComposition.name)
                )
                assert parameters["status_1"] == "ACTIVE"
                matches = [
                    row
                    for row in self.compositions
                    if row.organization_id == self.project.organization_id
                    and row.status == "ACTIVE"
                ]
            assert parameters["presentation_1"] == "module"
            assert statement.compare(expected)
            matches = [
                row
                for row in matches
                if (
                    self.project_present
                    and self.project.id == parameters["project_id_1"]
                    and row.presentation == "module"
                    and SkillComposition not in self.missing
                    and any(
                        item.composition_id == row.id
                        and item.project_id == parameters["project_id_1"]
                        for item in self.enablements
                    )
                )
            ]
            self.queries.append(str(statement.compile(dialect=POSTGRESQL_DIALECT)))
            self.emit("composition")
            return LifecycleResult(sorted(matches, key=lambda row: row.name))
        if entity is ProjectComposition:
            expected_enablements = (
                select(ProjectComposition)
                .where(
                    ProjectComposition.composition_id == parameters["composition_id_1"],
                )
                .order_by(ProjectComposition.id)
                .with_for_update()
                .execution_options(
                    populate_existing=True,
                )
            )
            assert statement.compare(expected_enablements)
            enablements = sorted(
                (
                    row
                    for row in self.enablements
                    if row.composition_id == parameters["composition_id_1"]
                ),
                key=lambda row: row.id,
            )
            self.queries.append(str(statement.compile(dialect=POSTGRESQL_DIALECT)))
            self.emit("enablements")
            return LifecycleResult(enablements)
        raise AssertionError(f"Unexpected scalars model: {entity}")

    async def execute(self, statement: Select[tuple[Any, ...]]) -> LifecycleResult:
        """表示 query の両組織条件を検証し、版の公開状態を読み取り条件へ足さない。"""
        parameters = statement.compile().params
        expected = (
            select(
                SkillVersion.id,
                Skill.id,
                Skill.key,
                Skill.name,
                SkillVersion.version,
            )
            .join(Skill, SkillVersion.skill_id == Skill.id)
            .join(
                SkillSource,
                SkillSource.id == SkillVersion.skill_source_id,
            )
            .where(
                SkillVersion.id.in_(parameters["id_2"]),
                Skill.organization_id == parameters["organization_id_1"],
                SkillSource.organization_id == parameters["organization_id_2"],
            )
        )
        assert statement.compare(expected)
        rows = [
            (version.id, self.skill.id, self.skill.key, self.skill.name, version.version)
            for version in self.versions
            if (
                version.id in parameters["id_2"]
                and version.skill_id == self.skill.id
                and version.skill_source_id == self.source.id
                and self.skill.organization_id == parameters["organization_id_1"]
                and self.source.organization_id == parameters["organization_id_2"]
            )
        ]
        self.queries.append(str(statement.compile(dialect=POSTGRESQL_DIALECT)))
        self.emit("projection")
        return LifecycleResult(rows)

    def add(self, row: SkillComposition | ProjectComposition) -> None:
        """親 flush 前に Project 関係を書かず、旧有効化監査を更新しないと確認する。"""
        if isinstance(row, SkillComposition):
            self.compositions.append(row)
            self.composition = row
            self.module_id = row.id
        else:
            assert row.composition_id in self.flushed_compositions
            assert row.project_id == self.project.id
            self.enablements.append(row)
        self.mutations.append(f"add-{type(row).__name__}")

    async def flush(self) -> None:
        """relationship の FK 同期と保存順だけを合成し、業務資格は判定しない。"""
        for composition in self.compositions:
            for item in composition.items:
                item.composition_id = composition.id
            self.flushed_compositions.add(composition.id)
        self.emit("flush")

    async def delete(self, row: SkillComposition | ProjectComposition) -> None:
        """要求された元行だけを保存集合から除き、他 Project の行は残す。"""
        if isinstance(row, SkillComposition):
            assert row in self.compositions
            self.compositions.remove(row)
        else:
            assert row in self.enablements
            self.enablements.remove(row)
        name = f"delete-{type(row).__name__}"
        self.mutations.append(name)
        self.emit(name)
