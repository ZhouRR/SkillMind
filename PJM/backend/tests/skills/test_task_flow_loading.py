"""実 Repository/service/projector を接続し、精確版の認可と read-only 境界を検査する。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.db.models import (
    RuntimeManifest,
    Skill,
    SkillInterpretation,
    SkillSource,
    SkillVersion,
)
from projectmind.skills.domain import PublishedTaskNotFoundError, SkillVersionNotFoundError
from projectmind.skills.repository import SkillRepository
from projectmind.skills.resource_binding import ProjectResourceCandidate
from projectmind.skills.service import SkillService, TaskFlowPreviewResult
from projectmind.skills.task_flow_preview import TaskFlowPreviewInvalidError
from tests.skills.task_flow_fixtures import make_task_flow_source

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


class BindingResult:
    """実 SELECT の active/version/org 条件で絞った aggregate だけを返す。"""

    def __init__(self, row: tuple[Skill, SkillVersion, RuntimeManifest] | None) -> None:
        """存在しない binding も通常 SELECT の None として保存する。"""

        self.row = row

    def one_or_none(self) -> tuple[Skill, SkillVersion, RuntimeManifest] | None:
        """唯一の exact binding を返し、新しい版へ fallback しない。"""

        return self.row


class FlowSession:
    """SQL の形を検査する読み取り専用 fake。add/flush/commit は提供しない。"""

    def __init__(self) -> None:
        """共有合成 source から独立した ORM 保存行を作る。"""

        source = make_task_flow_source()
        self.organization_id = uuid4()
        self.project_id = source.project_id
        self.project_organization_id = self.organization_id
        self.enabled = True
        self.skill = Skill(
            id=source.skill_id,
            organization_id=self.organization_id,
            key=source.skill_key,
        )
        self.version = SkillVersion(
            id=source.skill_version_id,
            skill_id=source.skill_id,
            skill_source_id=source.skill_source_id,
            interpretation_id=source.interpretation_id,
            status="PUBLISHED",
            version=source.version,
        )
        self.manifest = RuntimeManifest(
            skill_version_id=source.skill_version_id,
            interpretation_id=source.interpretation_id,
            manifest_version=source.manifest["manifest_version"],
            manifest_json=deepcopy(source.manifest),
            checksum=source.manifest_checksum,
        )
        self.source = SkillSource(
            id=source.skill_source_id,
            organization_id=self.organization_id,
            content_hash=source.source_hash,
            source_file_index_json=deepcopy(source.source_file_index),
            source_snapshot_json=deepcopy(source.source_snapshot),
        )
        self.interpretation = SkillInterpretation(
            id=source.interpretation_id,
            skill_source_id=source.skill_source_id,
            status="PREVIEW_READY",
            origin="model",
            interpreter_version=source.interpreter_version,
        )
        self.missing: set[type[SkillSource] | type[SkillInterpretation]] = set()
        self.queries: list[str] = []
        self.loads: list[tuple[object, object]] = []
        self.closed = False

    async def __aenter__(self) -> Self:
        """Service が開始した Session context を返す。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """書き込みなしで context 終了を記録する。"""

        self.closed = True

    async def execute(self, statement: Select[tuple[Any, ...]]) -> BindingResult:
        """実 SQL の主要 WHERE/JOIN を確認し、その exact scope の可視性を模擬する。"""

        # 公開 dialect factory は動的 export なので、constructor 境界だけを型付けする。
        compiled = statement.compile(dialect=cast(Callable[[], Dialect], dialect)())
        sql = str(compiled)
        self.queries.append(sql)
        for condition in (
            "project_skill_versions.project_id =",
            "project_skill_versions.skill_version_id =",
            "project_skill_versions.disabled_at IS NULL",
            "skill_versions.status =",
            "projects.organization_id = skill_sources.organization_id",
            "skills.organization_id = skill_sources.organization_id",
            "runtime_manifests.skill_version_id = skill_versions.id",
        ):
            assert condition in sql
        assert "FOR UPDATE" not in sql
        assert compiled.params == {
            "project_id_1": self.project_id,
            "skill_version_id_1": self.version.id,
            "status_2": "PUBLISHED",
        }
        visible = (
            self.enabled
            and self.version.status == "PUBLISHED"
            and self.project_organization_id == self.source.organization_id
            and self.skill.organization_id == self.source.organization_id
        )
        return BindingResult((self.skill, self.version, self.manifest) if visible else None)

    async def get(
        self,
        model: object,
        identity: object,
        *,
        populate_existing: bool,
    ) -> SkillSource | SkillInterpretation | None:
        """実主キー取得を記録し、欠損や不正 FK を隠して補完しない。"""

        assert populate_existing is True
        self.loads.append((model, identity))
        if model in self.missing:
            return None
        if model is SkillSource:
            assert identity == self.version.skill_source_id
            return self.source
        assert model is SkillInterpretation
        assert identity == self.version.interpretation_id
        return self.interpretation

    def repository(self) -> SkillRepository:
        """実 Repository への最小 AsyncSession seam を明示して cast する。"""

        return SkillRepository(cast(AsyncSession, self))

    def service(self, catalog: FlowCatalog | None = None) -> SkillService:
        """Interpreter/storage を渡さず、実 service の session factory だけを差し替える。"""

        return SkillService(
            cast(async_sessionmaker[AsyncSession], lambda: self),
            CONTRACTS,
            resource_catalog=catalog,
        )

    async def preview(self, *, catalog: FlowCatalog | None = None) -> TaskFlowPreviewResult:
        """公開 use case を原 Project/版/actor organization で呼ぶ。"""

        return await self.service(catalog).get_task_flow_preview(
            organization_id=self.organization_id,
            project_id=self.project_id,
            skill_version_id=self.version.id,
            task_key="explain",
        )

    def rehash(self) -> None:
        """テストで変更した合法 JSON の原 checksum を明示的に再計算する。"""

        self.manifest.checksum = "sha256:" + sha256_hex(canonical_json(self.manifest.manifest_json))


class FlowCatalog:
    """現在の Project 候補の観測だけを返し、資源作成や選択を行わない。"""

    def __init__(self) -> None:
        """候補ゼロと次回の候補追加を区別して保存する。"""

        self.calls: list[UUID] = []
        self.values: tuple[ProjectResourceCandidate, ...] = ()

    async def candidates(self, *, project_id: UUID) -> tuple[ProjectResourceCandidate, ...]:
        """正確な Project scope の列挙呼び出しを記録する。"""

        self.calls.append(project_id)
        return self.values


@pytest.mark.asyncio
async def test_real_loader_keeps_raw_source_and_does_not_write() -> None:
    """実 binding SQL と FK を検証し、JSON の補正・alias と書き込みを発生させない。"""

    session = FlowSession()
    source = await session.repository().get_task_flow_preview_source(
        organization_id=session.organization_id,
        project_id=session.project_id,
        skill_version_id=session.version.id,
    )
    assert source.source_file_index == session.source.source_file_index_json
    assert source.source_snapshot == session.source.source_snapshot_json
    source.source_snapshot[0]["content"] = "caller mutation"
    source.manifest["identity"]["skill_key"] = "caller-mutation"
    assert session.source.source_snapshot_json[0]["content"] != "caller mutation"
    assert session.manifest.manifest_json["identity"]["skill_key"] == "reading-guide"
    assert len(session.queries) == 1
    assert len(session.loads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["disabled", "draft", "deprecated", "foreign_project", "foreign_skill"]
)
async def test_exact_published_scope_hides_missing_or_foreign_binding(change: str) -> None:
    """無効版や他 Organization を同じ not-found にし、source を読み始めない。"""

    session = FlowSession()
    if change == "disabled":
        session.enabled = False
    elif change in {"draft", "deprecated"}:
        session.version.status = change.upper()
    elif change == "foreign_project":
        session.project_organization_id = uuid4()
    else:
        session.skill.organization_id = uuid4()
    with pytest.raises(SkillVersionNotFoundError):
        await session.preview()
    assert session.loads == []


@pytest.mark.asyncio
async def test_actor_organization_is_checked_before_reading_private_source() -> None:
    """Project binding があっても別 actor Organization には raw source を渡さない。"""

    session = FlowSession()
    with pytest.raises(SkillVersionNotFoundError):
        await session.repository().get_task_flow_preview_source(
            organization_id=uuid4(),
            project_id=session.project_id,
            skill_version_id=session.version.id,
        )
    assert session.loads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "source_missing",
        "interpretation_missing",
        "source_id",
        "interpretation_id",
        "interpretation_source",
        "interpretation_status",
        "manifest_version_fk",
        "manifest_interpretation",
        "manifest_format_column",
        "parser_blueprint",
        "raw_index",
        "raw_snapshot",
        "checksum",
    ],
)
async def test_saved_integrity_is_not_coerced_or_repaired(change: str) -> None:
    """合法形状の FK 差替えも raw JSON の破損も同じ静的 integrity error にする。"""

    session = FlowSession()
    if change == "source_missing":
        session.missing.add(SkillSource)
    elif change == "interpretation_missing":
        session.missing.add(SkillInterpretation)
    elif change == "source_id":
        session.source.id = uuid4()
    elif change == "interpretation_id":
        session.interpretation.id = uuid4()
    elif change == "interpretation_source":
        session.interpretation.skill_source_id = uuid4()
    elif change == "interpretation_status":
        session.interpretation.status = "FAILED"
    elif change == "manifest_version_fk":
        session.manifest.skill_version_id = uuid4()
    elif change == "manifest_interpretation":
        session.manifest.interpretation_id = uuid4()
    elif change == "manifest_format_column":
        session.manifest.manifest_version = "projectmind/future"
    elif change == "parser_blueprint":
        session.interpretation.origin = "deterministic_parser"
    elif change == "raw_index":
        session.source.source_file_index_json.append({"path": 123})
    elif change == "raw_snapshot":
        cast(dict[str, object], session.source.source_snapshot_json[0])["content"] = 123
    else:
        session.manifest.checksum = "sha256:" + "0" * 64
    catalog = FlowCatalog()
    with pytest.raises(
        TaskFlowPreviewInvalidError, match=r"^The saved task flow preview is invalid\.$"
    ):
        await session.preview(catalog=catalog)
    assert catalog.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_null", [False, True])
async def test_legacy_parser_without_blueprint_is_not_declared(explicit_null: bool) -> None:
    """古い parser の未宣言は空計画とし、欠けた source や候補を捏造しない。"""

    session = FlowSession()
    session.interpretation.origin = "deterministic_parser"
    if explicit_null:
        session.manifest.manifest_json["capability_blueprint"] = None
    else:
        session.manifest.manifest_json.pop("capability_blueprint")
    session.source.source_snapshot_json = []
    session.rehash()
    catalog = FlowCatalog()
    value = await session.preview(catalog=catalog)
    assert value.preview.status == "NOT_DECLARED"
    assert value.preview.plan is None
    assert value.readiness is None
    assert catalog.calls == []


@pytest.mark.asyncio
async def test_readiness_is_current_full_blueprint_and_not_preview_identity() -> None:
    """選択 Task 外の共有資源も照合し、候補変化では不変な plan checksum を変えない。"""

    session = FlowSession()
    catalog = FlowCatalog()
    first = await session.preview(catalog=catalog)
    assert first.readiness is not None
    assert [item.key for item in first.readiness.requirements] == ["source", "notes", "tracker"]
    catalog.values = (
        ProjectResourceCandidate(
            key="doc-1",
            kind="document",
            provider="documents",
            label="A document",
            capabilities=(),
        ),
    )
    second = await session.preview(catalog=catalog)
    assert second.readiness != first.readiness
    assert second.preview.to_json() == first.preview.to_json()
    assert catalog.calls == [session.project_id, session.project_id]
    assert session.closed


@pytest.mark.asyncio
async def test_missing_task_does_not_observe_resources_or_fallback_to_other_task() -> None:
    """精確版に存在しない task_key を他 Task や最新版本で補完しない。"""

    session = FlowSession()
    catalog = FlowCatalog()
    with pytest.raises(PublishedTaskNotFoundError):
        await session.service(catalog).get_task_flow_preview(
            organization_id=session.organization_id,
            project_id=session.project_id,
            skill_version_id=session.version.id,
            task_key="missing",
        )
    assert catalog.calls == []
