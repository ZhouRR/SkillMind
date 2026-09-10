"""Skill の四管理操作を実 service/repository/資格判定と局部 SQL 応答で接続する。

資産 rollback は合成であり、PostgreSQL の lock 競争/commit を代替しない。
Project/User/AuthSession の外部失効は自分の資産 rollback で復活させない。
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self, cast
from uuid import UUID, uuid4

from sqlalchemy import Delete, Select, and_, select
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import (
    ChangeProposal,
    FrontendModuleVersion,
    Project,
    ProjectSkillVersion,
    RunSkillSnapshot,
    RuntimeManifest,
    Skill,
    SkillCompositionItem,
    SkillSource,
    SkillVersion,
    TaskSchedule,
    TaskScheduleOccurrence,
)
from skillmind.skills.domain import StoredProjectSkillVersion, StoredSkillVersion
from skillmind.skills.repository import SkillRepository
from tests.skills.test_skill_publication_authorization import (
    AuthorizationSession,
    ControlledTransaction,
)

Operation = Literal["deprecate", "delete", "enable", "disable"]
OPERATIONS: tuple[Operation, ...] = ("deprecate", "delete", "enable", "disable")
# SQLAlchemy の公開 factory は型宣言が無いが、引数無しで Dialect を生成する境界である。
POSTGRESQL_DIALECT = cast(Callable[[], Dialect], dialect)()
REFERENCE_MODELS = (
    RunSkillSnapshot,
    ChangeProposal,
    SkillCompositionItem,
    TaskSchedule,
    TaskScheduleOccurrence,
    FrontendModuleVersion,
)


class LifecycleResult:
    """ORM 行/ID を返す本番 SELECT の動的な結果境界だけを表す。"""

    def __init__(self, rows: list[Any]) -> None:
        """実 WHERE 条件を適用済みの行を受け取り、未知 query を成功にしない。"""
        self.rows = rows

    def one_or_none(self) -> Any:
        """一意検索で複数行を黙って先頭へ縮めない。"""
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def all(self) -> list[Any]:
        """SQL 応答を列挙し、権限の判定や投影は本番へ委ねる。"""
        return self.rows


class LifecycleTransaction(ControlledTransaction):
    """原 Skill 資産の rollback に、追加/削除された Project binding の復元を加える。"""

    def __init__(self, session: LifecycleSession) -> None:
        """資格行は共有 transaction と同様に rollback 対象に含めない。"""
        super().__init__(session)
        self.lifecycle = session
        self.bindings: list[tuple[ProjectSkillVersion, dict[str, Any]]] = []
        self.target: ProjectSkillVersion | None = None

    async def __aenter__(self) -> Self:
        """既存 binding の原値と元の集合を記録する。"""
        await super().__aenter__()
        self.target = self.lifecycle.binding
        self.bindings = [
            (row, self.lifecycle.binding_values(row)) for row in self.lifecycle.bindings
        ]
        return self

    def restore(self) -> None:
        """新しく追加した binding は保存集合から除き、既存の停用/削除は原値へ戻す。"""
        super().restore()
        for row, values in self.bindings:
            for key, value in values.items():
                setattr(row, key, deepcopy(value))
        self.lifecycle.bindings[:] = [row for row, _ in self.bindings]
        assert self.target is not None
        self.lifecycle.binding = self.target


class LifecycleSession(AuthorizationSession):
    """公開 API からも注入できる、四操作と原持続会話の合成 aggregate。"""

    def __init__(self, operation: Operation) -> None:
        """削除は廃止版、停用は既存 binding、それ以外は未操作の合法資産を準備する。"""
        super().__init__()
        self.operation = operation
        now = datetime.now(UTC)
        # HTTP 回帰は実時計を使う。期限単体 test だけ明示的な clock に置換する。
        self.auth_session.idle_expires_at = now + timedelta(minutes=30)
        self.auth_session.absolute_expires_at = now + timedelta(hours=8)
        self.project = Project(
            id=uuid4(),
            organization_id=self.organization_id,
            key="skill-lifecycle",
            name="Synthetic lifecycle project",
            description="",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self.project_present = True
        self.skill.status = "PUBLISHED"
        self.version.status = "DEPRECATED" if operation == "delete" else "PUBLISHED"
        self.version.published_by = self.user.id
        self.version.published_at = self.version.created_at
        self.binding = ProjectSkillVersion(
            id=uuid4(),
            project_id=self.project.id,
            skill_version_id=self.version.id,
            enabled_by=self.user.id,
            enabled_at=now - timedelta(days=1),
            disabled_at=None,
        )
        self.bindings: list[ProjectSkillVersion] = (
            [self.binding] if operation in {"disable", "delete"} else []
        )
        self.references: dict[type[Any], set[UUID]] = {model: set() for model in REFERENCE_MODELS}
        self.mutations: list[str] = []

    def begin(self) -> LifecycleTransaction:
        """既存の成功/拒否/commit 不明観測を binding の保存集合にも適用する。"""
        return LifecycleTransaction(self)

    @staticmethod
    def binding_values(row: ProjectSkillVersion) -> dict[str, Any]:
        """同じ model class の複数 binding を ID ごとに比較できるよう保存列を写す。"""
        return deepcopy({column.key: getattr(row, column.key) for column in row.__table__.columns})

    def frozen_values(self) -> dict[str, object]:
        """原資産と全 binding を比較し、Project の外部帰档は復元対象から分離する。"""
        return {
            **super().frozen_values(),
            "ProjectSkillVersion": [self.binding_values(row) for row in self.bindings],
        }

    async def scalar(self, statement: Select[tuple[Any, ...]]) -> Any:
        """Project SHARE または参照数の精確 SQL を処理し、他は元資格 seam へ渡す。"""
        entity = statement.column_descriptions[0].get("entity")
        parameters = statement.compile().params
        if entity is SkillVersion:
            # 現在の task binding 用 SHARE は管理 UPDATE と別の query である。
            assert set(parameters) == {
                "id_1",
                "status_1",
                "organization_id_1",
                "organization_id_2",
                "id_2",
            }
            expected = (
                select(SkillVersion)
                .join(SkillSource, SkillSource.id == SkillVersion.skill_source_id)
                .join(Skill, Skill.id == SkillVersion.skill_id)
                .join(Project, Project.organization_id == Skill.organization_id)
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
            assert statement.compare(expected)
            assert parameters["status_1"] == "PUBLISHED"
            sql = str(statement.compile(dialect=POSTGRESQL_DIALECT))
            assert "FOR SHARE OF skill_versions" in sql
            assert statement.get_execution_options()["populate_existing"] is True
            found = (
                self.version in self.rows
                and SkillVersion not in self.missing
                and self.source in self.rows
                and SkillSource not in self.missing
                and self.skill in self.rows
                and Skill not in self.missing
                and self.version.id == parameters["id_1"]
                and self.version.status == parameters["status_1"]
                and self.source.id == self.version.skill_source_id
                and self.skill.id == self.version.skill_id
                and self.skill.organization_id == parameters["organization_id_1"]
                and self.source.organization_id == parameters["organization_id_2"]
                and self.project_present
                and self.project.id == parameters["id_2"]
                and self.project.organization_id == self.skill.organization_id
            )
            self.queries.append(sql)
            self.emit("task-version")
            return self.version if found else None
        if entity is ProjectSkillVersion:
            assert statement.whereclause is not None
            assert set(parameters) == {"project_id_1", "skill_version_id_1"}
            assert statement.whereclause.compare(
                and_(
                    ProjectSkillVersion.project_id == parameters["project_id_1"],
                    ProjectSkillVersion.skill_version_id == parameters["skill_version_id_1"],
                )
            )
            sql = str(statement.compile(dialect=POSTGRESQL_DIALECT))
            assert "FOR SHARE" in sql and "FOR UPDATE" not in sql
            assert statement.get_execution_options()["populate_existing"] is True
            matches = [
                row
                for row in self.bindings
                if row.project_id == parameters["project_id_1"]
                and row.skill_version_id == parameters["skill_version_id_1"]
            ]
            assert len(matches) <= 1
            self.queries.append(sql)
            self.emit("task-binding")
            return matches[0] if matches else None
        if entity is Project:
            assert statement.column_descriptions[0]["expr"] is Project
            assert statement.whereclause is not None
            assert statement.whereclause.compare(
                and_(
                    Project.id == parameters["id_1"],
                    Project.organization_id == parameters["organization_id_1"],
                )
            )
            sql = str(statement.compile(dialect=POSTGRESQL_DIALECT))
            assert "FOR SHARE" in sql and "FOR KEY SHARE" not in sql
            assert statement.get_execution_options()["populate_existing"] is True
            self.queries.append(sql)
            found = (
                self.project_present
                and self.project.id == parameters["id_1"]
                and self.project.organization_id == parameters["organization_id_1"]
            )
            result = self.project if found else None
            self.emit("project")
            return result
        tables = statement.get_final_froms()
        reference_model = next(
            (model for model in REFERENCE_MODELS if tables == [model.__table__]), None
        )
        if reference_model is not None:
            assert len(statement.column_descriptions) == 1
            assert str(statement.column_descriptions[0]["expr"]) == "count(*)"
            assert set(parameters) == {"skill_version_id_1"}
            assert statement.whereclause is not None
            assert statement.whereclause.compare(
                reference_model.skill_version_id == parameters["skill_version_id_1"]
            )
            self.queries.append(str(statement))
            reference_count = int(
                parameters["skill_version_id_1"] in self.references[reference_model]
            )
            self.emit(f"references-{reference_model.__name__}")
            return reference_count
        return await super().scalar(statement)

    async def scalars(self, statement: Select[tuple[Any, ...]]) -> Any:
        """精確 Project ID と ProjectSkillVersion UPDATE を本番 predicate で検索する。"""
        entity = statement.column_descriptions[0].get("entity")
        parameters = statement.compile().params
        if entity is Project:
            # InstrumentedAttribute ではなく、SELECT が実際に投影する SQL 列を比較する。
            assert list(statement.selected_columns) == [Project.__table__.c.id]
            assert set(parameters) == {"id_1", "organization_id_1"}
            assert statement.whereclause is not None
            assert statement.whereclause.compare(
                and_(
                    Project.id == parameters["id_1"],
                    Project.organization_id == parameters["organization_id_1"],
                )
            )
            found = (
                self.project_present
                and self.project.id == parameters["id_1"]
                and self.project.organization_id == parameters["organization_id_1"]
            )
            self.emit("project-identity")
            return LifecycleResult([self.project.id] if found else [])
        if entity is ProjectSkillVersion:
            assert statement.column_descriptions[0]["expr"] is ProjectSkillVersion
            assert set(parameters) == {"project_id_1", "skill_version_id_1"}
            assert statement.whereclause is not None
            assert statement.whereclause.compare(
                and_(
                    ProjectSkillVersion.project_id == parameters["project_id_1"],
                    ProjectSkillVersion.skill_version_id == parameters["skill_version_id_1"],
                )
            )
            assert "FOR UPDATE" in str(statement)
            assert statement.get_execution_options()["populate_existing"] is True
            self.queries.append(str(statement))
            result = [
                row
                for row in self.bindings
                if row.project_id == parameters["project_id_1"]
                and row.skill_version_id == parameters["skill_version_id_1"]
            ]
            self.emit("binding")
            return LifecycleResult(result)
        return await super().scalars(statement)

    def add(self, row: ProjectSkillVersion) -> None:
        """有効化だけが新 binding を追加し、既存監査を書き換えないことを観測する。"""
        assert isinstance(row, ProjectSkillVersion)
        assert row.project_id == self.project.id and row.skill_version_id == self.version.id
        self.binding = row
        self.bindings.append(row)
        self.mutations.append("add-binding")
        self.emit("add-binding")

    async def execute(self, statement: Delete) -> None:
        """原版を指す binding だけを削除し、同 Project の別版を巻き込まない。"""
        assert isinstance(statement, Delete)
        assert statement.table.compare(ProjectSkillVersion.__table__)
        parameters = statement.compile().params
        assert set(parameters) == {"skill_version_id_1"}
        assert statement.whereclause is not None
        assert statement.whereclause.compare(
            ProjectSkillVersion.skill_version_id == parameters["skill_version_id_1"]
        )
        self.bindings[:] = [
            row for row in self.bindings if row.skill_version_id != parameters["skill_version_id_1"]
        ]
        self.queries.append(str(statement))
        self.mutations.append("delete-bindings")
        self.emit("delete-bindings")

    async def delete(self, row: RuntimeManifest | SkillVersion) -> None:
        """同じ aggregate の Manifest/Version だけを削除待ち集合から除く。"""
        assert row is self.manifest or row is self.version
        assert any(saved is row for saved in self.rows)
        self.rows = tuple(saved for saved in self.rows if saved is not row)
        name = f"delete-{type(row).__name__}"
        self.mutations.append(name)
        self.emit(name)

    async def operate(self) -> StoredSkillVersion | StoredProjectSkillVersion | None:
        """必須の原 UserAccess を四操作それぞれの本番入口へ渡す。"""
        service = self.service()
        if self.operation == "deprecate":
            return await service.deprecate_skill_version(
                access=self.access, skill_version_id=self.version.id
            )
        if self.operation == "delete":
            await service.delete_skill_version(access=self.access, skill_version_id=self.version.id)
            return None
        if self.operation == "enable":
            return await service.enable_project_skill_version(
                access=self.access, project_id=self.project.id, skill_version_id=self.version.id
            )
        return await service.disable_project_skill_version(
            access=self.access, project_id=self.project.id, skill_version_id=self.version.id
        )

    async def check_current_binding(self) -> None:
        """本番の二段 SHARE query を直接呼び、guard の結果を mock しない。"""
        await SkillRepository(cast(AsyncSession, self)).require_current_task_binding(
            organization_id=self.access.actor.organization_id,
            project_id=self.project.id,
            skill_version_id=self.version.id,
        )
