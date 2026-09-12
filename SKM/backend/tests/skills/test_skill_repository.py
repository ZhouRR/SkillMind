"""SkillRepository の不変 source と interpretation 保存を検証する。

低層の authorize callback は scope/状態テストの時刻だけを返す。原会話の資格と
transaction 内の再検証は実 UserRepository を使う service/API 回帰で別に確認する。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import ProjectSkillVersion, SkillInterpretation, SkillSource
from skillmind.skills.capability_blueprint import CapabilityBlueprintError
from skillmind.skills.domain import (
    CreateSkillVersionDraftCommand,
    InlineSkillFile,
    SaveModelInterpretationCommand,
    SaveSkillPreviewCommand,
    SkillInterpretationNotFoundError,
    SkillInterpretationNotReadyError,
    SkillInterpretationStatus,
    SkillPreview,
    SkillSourceNotFoundError,
    SkillVersionDeleteBlockedError,
    SkillVersionEnablementConflictError,
    SkillVersionEnablementNotFoundError,
    SkillVersionNotFoundError,
    SkillVersionStatus,
    SkillVersionTransitionError,
)
from skillmind.skills.manifest_gate import ManifestValidator
from skillmind.skills.repository import SkillRepository
from skillmind.skills.service import _schema_failure_detail

ORGANIZATION_ID = UUID("00000000-0000-4000-8000-0000000000a1")


def _command() -> SaveSkillPreviewCommand:
    """Repository test 用の deterministic preview command を生成する。"""

    return SaveSkillPreviewCommand(
        organization_id=ORGANIZATION_ID,
        imported_by=uuid4(),
        name="Ticket Reviewer",
        source_type="directory",
        source_hash="sha256:" + ("1" * 64),
        source_files=(InlineSkillFile(path="SKILL.md", content="# Ticket Reviewer\n"),),
        interpreter_version="deterministic-parser/1.0.0",
        compatibility_level="assisted",
        confidence=0.25,
        diagnostics=({"severity": "warning", "code": "interpreter_required"},),
        checksum="sha256:" + ("2" * 64),
        preview=SkillPreview(
            normalized_package={"package_format": "skillmind.normalized/v1"},
            runtime_manifest_draft={"manifest_version": "skillmind/v1alpha1"},
        ),
    )


@pytest.mark.asyncio
async def test_save_preview_creates_source_and_interpretation() -> None:
    """初回保存が source snapshot と interpretation を同じ session に追加する。"""

    session = MagicMock(spec=AsyncSession)
    missing_source = MagicMock()
    missing_source.one_or_none.return_value = None
    # source 未登録 → interpretation 未登録、の順で引かれる。
    missing_interpretation = MagicMock()
    missing_interpretation.one_or_none.return_value = None
    session.scalars = AsyncMock(side_effect=[missing_source, missing_interpretation])
    command = _command()

    stored = await SkillRepository(session).save_preview(
        command, authorize=lambda: datetime(2026, 9, 10, tzinfo=UTC)
    )

    added = [call.args[0] for call in session.add.call_args_list]
    assert isinstance(added[0], SkillSource)
    assert isinstance(added[1], SkillInterpretation)
    assert added[0].source_snapshot_json == [{"path": "SKILL.md", "content": "# Ticket Reviewer\n"}]
    # Organization は認証境界で確定した command 値をそのまま不変 source へ刻む。
    assert added[0].organization_id == ORGANIZATION_ID
    assert added[1].model is None
    assert stored.skill_source_id == added[0].id
    assert stored.interpretation_id == added[1].id


@pytest.mark.asyncio
async def test_save_preview_reuses_existing_source_and_interpretation() -> None:
    """同一 hash/checksum の再保存が新しい不変 record を追加しない。"""

    command = _command()
    source = SkillSource(
        id=uuid4(),
        organization_id=command.organization_id,
        name=command.name,
        source_type=command.source_type,
        storage_uri="database://skill-sources/existing",
        content_hash=command.source_hash,
        source_version=None,
        imported_by=command.imported_by,
        source_snapshot_json=[],
        created_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    interpretation = SkillInterpretation(
        id=uuid4(),
        skill_source_id=source.id,
        origin="deterministic_parser",
        interpreter_version=command.interpreter_version,
        model=None,
        compatibility_level="assisted",
        status="PREVIEW_READY",
        summary="Existing",
        confidence=0.25,
        assumptions_json=[],
        questions_json=[],
        diagnostics_json=[],
        normalized_package_json=command.preview.normalized_package,
        manifest_draft_json=command.preview.runtime_manifest_draft,
        checksum=command.checksum,
        created_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    session = MagicMock(spec=AsyncSession)
    source_result = MagicMock()
    source_result.one_or_none.return_value = source
    interpretation_result = MagicMock()
    interpretation_result.one_or_none.return_value = interpretation
    session.scalars = AsyncMock(side_effect=[source_result, interpretation_result])

    stored = await SkillRepository(session).save_preview(
        command, authorize=lambda: datetime(2026, 9, 10, tzinfo=UTC)
    )

    session.add.assert_not_called()
    assert stored.skill_source_id == source.id
    assert stored.interpretation_id == interpretation.id


@pytest.mark.asyncio
async def test_get_interpretation_rejects_unknown_id() -> None:
    """存在しない interpretation を安定した domain error へ変換する。"""

    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=None)

    with pytest.raises(SkillInterpretationNotFoundError):
        await SkillRepository(session).get_interpretation(
            organization_id=ORGANIZATION_ID,
            interpretation_id=uuid4(),
        )


def _source(organization_id: object, source_id: object) -> SkillSource:
    """Interpret 用の永続 SkillSource snapshot を生成する。"""

    return SkillSource(
        id=source_id,
        organization_id=organization_id,
        name="Repository Reviewer",
        source_type="directory",
        storage_uri="database://skill-sources/existing",
        content_hash="sha256:" + ("1" * 64),
        source_version=None,
        imported_by=uuid4(),
        source_snapshot_json=[{"path": "SKILL.md", "content": "# Repository Reviewer\n"}],
        created_at=datetime(2026, 7, 8, tzinfo=UTC),
    )


def _model_command(organization_id: object, source_id: object) -> SaveModelInterpretationCommand:
    """PREVIEW_READY の model interpretation 保存 command を生成する。"""

    return SaveModelInterpretationCommand(
        organization_id=organization_id,  # type: ignore[arg-type]
        skill_source_id=source_id,  # type: ignore[arg-type]
        execution_key="sha256:" + ("e" * 64),
        interpreter_version="skillmind-skill-interpreter/1.0.0",
        model="claude-opus-4-8",
        status=SkillInterpretationStatus.PREVIEW_READY,
        compatibility_level="adapted",
        confidence=0.85,
        summary="Adapted repository review task.",
        assumptions=(),
        questions=(),
        diagnostics=(),
        normalized_package={"package_format": "skillmind.normalized/v1"},
        manifest_draft={"manifest_version": "skillmind/v1alpha1"},
        report={"report_version": "skillmind.skill-interpretation-report/v1"},
        execution={"model": "claude-opus-4-8", "error_code": None},
    )


@pytest.mark.asyncio
async def test_get_source_returns_snapshot_files() -> None:
    """Organization 所有 source の snapshot file を read model へ復元する。"""

    organization_id = uuid4()
    source_id = uuid4()
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=_source(organization_id, source_id))

    stored = await SkillRepository(session).get_source(
        organization_id=organization_id, skill_source_id=source_id
    )

    assert stored.source_files == (InlineSkillFile("SKILL.md", "# Repository Reviewer\n"),)
    assert stored.source_hash == "sha256:" + ("1" * 64)


@pytest.mark.asyncio
async def test_get_source_hides_other_organization_source() -> None:
    """別 Organization の source は存在しないものとして扱う。"""

    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=_source(uuid4(), uuid4()))

    with pytest.raises(SkillSourceNotFoundError):
        await SkillRepository(session).get_source(organization_id=uuid4(), skill_source_id=uuid4())


@pytest.mark.asyncio
async def test_save_model_interpretation_creates_immutable_record() -> None:
    """初回 model 実行が origin=model の不変 interpretation を追加する。"""

    organization_id = uuid4()
    source_id = uuid4()
    command = _model_command(organization_id, source_id)
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=_source(organization_id, source_id))
    missing = MagicMock()
    missing.one_or_none.return_value = None
    session.scalars = AsyncMock(return_value=missing)

    stored = await SkillRepository(session).save_model_interpretation(command)

    added = session.add.call_args_list[0].args[0]
    assert isinstance(added, SkillInterpretation)
    assert added.origin == "model"
    assert added.model == "claude-opus-4-8"
    assert added.checksum == command.execution_key
    assert added.report_json == command.report
    assert added.execution_json == command.execution
    assert stored.reused is False
    assert stored.execution_key == command.execution_key
    assert stored.error_code is None


@pytest.mark.asyncio
async def test_save_model_interpretation_reuses_same_execution_key() -> None:
    """同一 execution key の再実行が新しい record を追加しない。"""

    organization_id = uuid4()
    source_id = uuid4()
    command = _model_command(organization_id, source_id)
    existing = SkillInterpretation(
        id=uuid4(),
        skill_source_id=source_id,
        origin="model",
        interpreter_version=command.interpreter_version,
        model=command.model,
        compatibility_level="adapted",
        status=SkillInterpretationStatus.FAILED.value,
        summary="Model interpretation failed: timeout",
        confidence=0.0,
        assumptions_json=[],
        questions_json=[],
        diagnostics_json=[],
        normalized_package_json={},
        manifest_draft_json={},
        report_json=None,
        execution_json={"error_code": "timeout"},
        checksum=command.execution_key,
        created_at=datetime(2026, 7, 8, tzinfo=UTC),
    )
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=_source(organization_id, source_id))
    result = MagicMock()
    result.one_or_none.return_value = existing
    session.scalars = AsyncMock(return_value=result)

    stored = await SkillRepository(session).save_model_interpretation(command)

    session.add.assert_not_called()
    assert stored.reused is True
    assert stored.interpretation_id == existing.id
    assert stored.error_code == "timeout"


def _stored_schema_diagnostic(instance, schema, *, wrapped=False):
    """実 validator/writer の診断を使い、candidate 値の脱落を読取境界で確認する。"""

    error = next(Draft202012Validator(schema).iter_errors(instance))
    if wrapped:
        error = CapabilityBlueprintError("blueprint_schema_invalid", "/", error.message)
    return _schema_failure_detail(error)


@pytest.mark.parametrize(
    "execution,expected",
    [
        ({"validation_attempts": ["/tasks/0: required", "/tasks/0: type"]},
         ("/tasks/0: required", "/tasks/0: type")),
        ({"validation_attempts": []}, ()),
        ({"error_code": "schema_validation_failed", "detail": "/tasks/0: required"},
         ("/tasks/0: required",)),
        ({"error_code": "provider_error", "detail": "private upstream response"}, ()),
        ({}, ()),
        ({"validation_attempts": ["/tasks/0: enum " + "x" * 4081]}, ("/tasks/0: enum",)),
        ({"validation_attempts": ["/fields/0/min_length: contract_constraint_invalid"]},
         ("/fields/0/min_length: contract_constraint_invalid",)),
        ({"validation_attempts": ["/runtime_manifest_draft/tools/0/operation: enum"]},
         ("/runtime_manifest_draft/tools/0/operation: enum",)),
        ({"validation_attempts": ["/tasks/0: const 'synthetic-private-instance'"]},
         ("/tasks/0: const",)),
        ({"validation_attempts": ["/: additionalProperties unexpected=['private-field']"]},
         ("/: additionalProperties",)),
        ({"validation_attempts": ["/fields/0/: contract_field_invalid"]},
         ("/fields/0/: contract_field_invalid",)),
        ({"validation_attempts": ["candidate_generation:invalid_json"]},
         ("candidate_generation:invalid_json",)),
        ({"validation_attempts": ["A required rule must cite a source trace"]},
         ("A required rule must cite a source trace",)),
        ({"validation_attempts": ["'private-instance' is not one of ['allowed']"]}, ()),
        ({"validation_attempts": ["Duplicate key: private-instance"]}, ()),
        ({"validation_attempts": ["/tasks/0: private_instance"]}, ()),
        ({"validation_attempts": ["/private field: enum"]}, ()),
        ({"validation_attempts": ["/report/confidence/1234: enum"]}, ()),
        ({"validation_attempts": ["/: contract_private_instance"]}, ()),
        ({"validation_attempts": [_stored_schema_diagnostic(
            {"SyntheticOpaqueCredential927Z": "unused"}, {"additionalProperties": False}
        )]}, ("/: additionalProperties",)),
        ({"validation_attempts": [_stored_schema_diagnostic(
            {"SyntheticOpaqueCredential927Z": "private"},
            {"additionalProperties": {"type": "integer"}},
        )]}, ()),
        ({"validation_attempts": [_stored_schema_diagnostic(
            "private-instance", {"enum": ["allowed"]}, wrapped=True,
        )]}, ()),
        ({"validation_attempts": ["x" * 4097]}, ()),
        ({"validation_attempts": ["first", "second", "third"]}, ()),
        ({"validation_attempts": None, "detail": "not a fallback"}, ()),
        ({"validation_attempts": "/tasks/0: required"}, ()),
        ({"validation_attempts": {"raw_candidate": "private"}}, ()),
        ({"validation_attempts": ["safe", 1]}, ()),
        ({"validation_attempts": [""]}, ()),
        ({"validation_attempts": ["   "]}, ()),
        ({"validation_attempts": ["line\nprivate"]}, ()),
        ({"validation_attempts": ["line\x7fprivate"]}, ()),
        ({"validation_attempts": ["unexpected=['password=synthetic-secret']"]}, ()),
        ({"validation_attempts": ["-----BEGIN PRIVATE KEY-----"]}, ()),
    ],
)
async def test_execution_diagnostics_only_project_bounded_stored_strings(execution, expected):
    """旧診断も原 JSON を変えず読み、内部 payload・異常文字列は公開しない。"""

    organization_id, source_id = uuid4(), uuid4()
    source = _source(organization_id, source_id)
    original = {"parameters": {"private": "synthetic"}, "raw_candidate": "private", **execution}
    row = SkillInterpretation(
        id=uuid4(), skill_source_id=source_id, origin="model", model="test-model",
        interpreter_version="skillmind-skill-interpreter/1.0.0", status="FAILED",
        compatibility_level="assisted", confidence=0.0, summary="Failed",
        normalized_package_json={}, manifest_draft_json={}, report_json=None,
        execution_json=deepcopy(original), created_at=datetime(2026, 9, 12, tzinfo=UTC),
    )
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(side_effect=[row, source])
    stored = await SkillRepository(session).get_model_interpretation(
        organization_id=organization_id, interpretation_id=row.id
    )
    assert stored.validation_attempts == expected
    assert row.execution_json == original
    session.add.assert_not_called()
    session.flush.assert_not_called()


async def test_execution_diagnostics_cannot_be_read_from_another_organization():
    """診断を持つ行でも原 source の Organization 不一致は同じ 404 境界を守る。"""

    source = _source(uuid4(), uuid4())
    row = SkillInterpretation(
        id=uuid4(), skill_source_id=source.id, execution_json={"validation_attempts": ["private"]}
    )
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(side_effect=[row, source])
    with pytest.raises(SkillInterpretationNotFoundError):
        await SkillRepository(session).get_model_interpretation(
            organization_id=uuid4(), interpretation_id=row.id
        )
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_version_draft_rejects_non_preview_ready_interpretation() -> None:
    """FAILED interpretation から発行可能な DRAFT を作らせない。"""

    organization_id = uuid4()
    source_id = uuid4()
    interpretation = SkillInterpretation(
        id=uuid4(),
        skill_source_id=source_id,
        origin="model",
        interpreter_version="skillmind-skill-interpreter/1.0.0",
        model="claude-opus-4-8",
        compatibility_level="assisted",
        status=SkillInterpretationStatus.FAILED.value,
        summary="Model interpretation failed: schema_validation_failed",
        confidence=0.0,
        assumptions_json=[],
        questions_json=[],
        diagnostics_json=[],
        normalized_package_json={},
        manifest_draft_json={},
        report_json=None,
        execution_json={"error_code": "schema_validation_failed"},
        checksum="sha256:" + ("e" * 64),
        created_at=datetime(2026, 7, 8, tzinfo=UTC),
    )
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(side_effect=[interpretation, _source(organization_id, source_id)])
    empty_versions = MagicMock()
    empty_versions.one_or_none.return_value = None
    session.scalars = AsyncMock(return_value=empty_versions)
    command = CreateSkillVersionDraftCommand(
        organization_id=organization_id,
        interpretation_id=interpretation.id,
        manifest={"identity": {"skill_key": "repository-review"}},
        manifest_checksum="sha256:" + ("f" * 64),
        gate_passed=True,
        gate_findings=(),
        interpretation_diff={},
    )

    with pytest.raises(SkillInterpretationNotReadyError):
        # これは repository の状態検査だけを対象とし、実資格は service 回帰で検証する。
        await SkillRepository(session).create_version_draft(
            command, authorize=lambda: datetime.now(UTC)
        )

    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_list_published_task_descriptors_projects_joined_rows() -> None:
    """PUBLISHED version の join 結果を task descriptor へ投影する。"""

    skill = SimpleNamespace(id=uuid4(), key="repository-review", name="Repository Review")
    version = SimpleNamespace(
        id=uuid4(), version="1.0.0", published_at=datetime(2026, 7, 9, tzinfo=UTC)
    )
    manifest = SimpleNamespace(
        manifest_json={
            "capabilities": [{"key": "repository.review", "title": "Repository Review"}],
            "tasks": [
                {
                    "key": "review-change",
                    "capability": "repository.review",
                    "type": "immediate",
                    "input_schema": {"type": "object", "additionalProperties": False},
                    "output_schema": {"type": "object", "additionalProperties": False},
                    "input_schema_checksum": "sha256:" + ("b" * 64),
                    "output_schema_checksum": "sha256:" + ("c" * 64),
                    "workflow": "review-change-v1",
                    "view": "repository-review-report",
                }
            ],
            "ui": {"default_view": "repository-review-report", "views": []},
            # 発行済み manifest は必ず蓝图を持つ。欠けた行は discovery へ出さない。
            "capability_blueprint": {
                "blueprint_version": "skillmind.capability-blueprint/v1",
                "identity": {
                    "skill_key": "repository-review",
                    "source_hash": "sha256:" + ("a" * 64),
                    "interpretation_id": "00000000-0000-4000-8000-000000000123",
                    "interpreter_version": "skillmind-skill-interpreter/2.3.0",
                },
                "compatibility": {"level": "adapted"},
                "capabilities": [{"key": "repository.review", "title": "Repository Review"}],
                "tasks": [
                    {
                        "key": "review-change",
                        "capability": "repository.review",
                        "objective": "Review the selected change and report findings.",
                    }
                ],
                "resource_requirements": [],
                "guidance": {
                    "required_rules": [],
                    "recommended_steps": [],
                    "quality_criteria": [],
                    "prohibited_actions": [],
                },
                "source_traces": [
                    {
                        "target": "/tasks/0",
                        "path": "SKILL.md",
                        "line": 5,
                        "reason": "The Goal section defines the review objective.",
                    }
                ],
            },
        }
    )
    session = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.all.return_value = [(skill, version, manifest)]
    session.execute = AsyncMock(return_value=result)

    tasks = await SkillRepository(session).list_published_task_descriptors(project_id=uuid4())

    assert len(tasks) == 1
    assert tasks[0].skill_key == "repository-review"
    assert tasks[0].skill_version_id == version.id
    assert tasks[0].task_key == "review-change"
    assert tasks[0].title == "Repository Review"
    assert tasks[0].default_view == "repository-review-report"


def _binding_session(status: str) -> MagicMock:
    """有効化済み PUBLISHED aggregate の join 結果を返す session。"""

    project_id = uuid4()
    version = SimpleNamespace(
        id=uuid4(),
        skill_source_id=uuid4(),
        skill_id=uuid4(),
        status=status,
        version="1.0.0",
        published_at=datetime(2026, 7, 9, tzinfo=UTC),
    )
    skill = SimpleNamespace(
        id=version.skill_id,
        key="repository-review",
        name="Repository Review",
        description="Reviews a repository change.",
    )
    manifest = SimpleNamespace(
        skill_version_id=version.id, checksum="sha256:" + ("a" * 64), manifest_json={"tasks": []}
    )
    session = MagicMock(spec=AsyncSession)
    joined = MagicMock()
    joined.one_or_none.return_value = (skill, version, manifest) if status == "PUBLISHED" else None
    session.execute = AsyncMock(return_value=joined)
    session.project_id = project_id
    return session


@pytest.mark.asyncio
async def test_get_published_task_binding_returns_published_version() -> None:
    """PUBLISHED version は skill/version/manifest を返す。"""

    session = _binding_session("PUBLISHED")

    skill, version, manifest = await SkillRepository(session).get_published_task_binding(
        project_id=session.project_id, skill_version_id=uuid4()
    )

    assert skill.key == "repository-review"
    assert version.status == "PUBLISHED"
    assert manifest.checksum == "sha256:" + ("a" * 64)


@pytest.mark.asyncio
async def test_get_published_task_binding_hides_unpublished_version() -> None:
    """DRAFT version は実行不能として 404 相当へ畳み込む。"""

    session = _binding_session("DRAFT")

    with pytest.raises(SkillVersionNotFoundError):
        await SkillRepository(session).get_published_task_binding(
            project_id=session.project_id, skill_version_id=uuid4()
        )


def _version_aggregate(
    organization_id: UUID,
    *,
    status: SkillVersionStatus = SkillVersionStatus.PUBLISHED,
) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    """Version lifecycle test 用の最小 aggregate を生成する。"""

    now = datetime(2026, 7, 10, tzinfo=UTC)
    skill = SimpleNamespace(
        id=uuid4(),
        organization_id=organization_id,
        key="repository-review",
        name="Repository Review",
        description="Reviews a repository change.",
        status="PUBLISHED",
        updated_at=now,
    )
    version = SimpleNamespace(
        id=uuid4(),
        skill_source_id=uuid4(),
        interpretation_id=uuid4(),
        version="1.0.0",
        status=status.value,
        gate_report_json={"passed": True, "findings": [], "interpretation_diff": {}},
        published_by=uuid4(),
        published_at=now,
        created_at=now,
        updated_at=now,
    )
    manifest = SimpleNamespace(
        checksum="sha256:" + ("a" * 64),
        manifest_json={"manifest_version": "skillmind/v1alpha1"},
    )
    return skill, version, manifest


@pytest.mark.asyncio
async def test_enable_project_skill_version_creates_exact_binding() -> None:
    """同一 Organization の PUBLISHED 精確版だけを Project enablement として追加する。"""

    organization_id = uuid4()
    project_id = uuid4()
    enabled_by = uuid4()
    aggregate = _version_aggregate(organization_id)
    project_result = MagicMock()
    project_result.one_or_none.return_value = project_id
    missing_binding = MagicMock()
    missing_binding.one_or_none.return_value = None
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(side_effect=[project_result, missing_binding])
    repository = SkillRepository(session)
    repository._version_models = AsyncMock(return_value=aggregate)  # type: ignore[method-assign]

    stored = await repository.enable_project_skill_version(
        organization_id=organization_id,
        project_id=project_id,
        skill_version_id=aggregate[1].id,
        enabled_by=enabled_by,
        authorize=lambda: datetime.now(UTC),
    )

    binding = session.add.call_args.args[0]
    assert isinstance(binding, ProjectSkillVersion)
    assert binding.project_id == project_id
    assert binding.skill_version_id == aggregate[1].id
    assert stored.skill_version.skill_version_id == aggregate[1].id
    assert stored.disabled_at is None


@pytest.mark.asyncio
async def test_enable_project_skill_version_rejects_unpublished_version() -> None:
    """DRAFT は Organization に存在しても Project の実行面へ有効化できない。"""

    organization_id = uuid4()
    project_id = uuid4()
    project_result = MagicMock()
    project_result.one_or_none.return_value = project_id
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(return_value=project_result)
    repository = SkillRepository(session)
    repository._version_models = AsyncMock(  # type: ignore[method-assign]
        return_value=_version_aggregate(organization_id, status=SkillVersionStatus.DRAFT)
    )

    with pytest.raises(SkillVersionEnablementConflictError):
        await repository.enable_project_skill_version(
            organization_id=organization_id,
            project_id=project_id,
            skill_version_id=uuid4(),
            enabled_by=uuid4(),
            authorize=lambda: datetime.now(UTC),
        )
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_enable_project_skill_version_hides_foreign_project() -> None:
    """別 Organization の Project と不存在を同じ enablement 404 相当へ畳み込む。"""

    missing_project = MagicMock()
    missing_project.one_or_none.return_value = None
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(return_value=missing_project)

    with pytest.raises(SkillVersionEnablementNotFoundError):
        await SkillRepository(session).enable_project_skill_version(
            organization_id=uuid4(),
            project_id=uuid4(),
            skill_version_id=uuid4(),
            enabled_by=uuid4(),
            authorize=lambda: datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_publish_does_not_reactivate_deprecated_version() -> None:
    """DEPRECATED 版を publish API で PUBLISHED へ戻す lifecycle 迂回を拒否する。"""

    organization_id = uuid4()
    aggregate = _version_aggregate(
        organization_id,
        status=SkillVersionStatus.DEPRECATED,
    )
    session = MagicMock(spec=AsyncSession)
    repository = SkillRepository(session)
    repository._version_models = AsyncMock(return_value=aggregate)  # type: ignore[method-assign]

    with pytest.raises(SkillVersionTransitionError):
        await repository.publish_skill_version(
            organization_id=organization_id,
            skill_version_id=aggregate[1].id,
            published_by=uuid4(),
            accepted_warnings=frozenset(),
            validate=ManifestValidator(Path(__file__).resolve().parents[3] / "contracts").evaluate,
            authorize=lambda: datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_delete_skill_version_rejects_published_versions() -> None:
    """PUBLISHED 版の削除を拒否し、廃止手順を経ない除去経路を塞ぐ。"""

    organization_id = uuid4()
    aggregate = _version_aggregate(organization_id, status=SkillVersionStatus.PUBLISHED)
    session = MagicMock(spec=AsyncSession)
    repository = SkillRepository(session)
    repository._version_models = AsyncMock(return_value=aggregate)  # type: ignore[method-assign]

    with pytest.raises(SkillVersionDeleteBlockedError):
        await repository.delete_skill_version(
            organization_id=organization_id,
            skill_version_id=aggregate[1].id,
            authorize=lambda: datetime.now(UTC),
        )
    session.delete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version_status", [SkillVersionStatus.DRAFT, SkillVersionStatus.DEPRECATED]
)
async def test_delete_skill_version_rejects_versions_referenced_by_runs(
    version_status: SkillVersionStatus,
) -> None:
    """Run snapshot が指す frozen Manifest を消させず、監査の説明可能性を守る。"""

    organization_id = uuid4()
    aggregate = _version_aggregate(organization_id, status=version_status)
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=1)
    repository = SkillRepository(session)
    repository._version_models = AsyncMock(return_value=aggregate)  # type: ignore[method-assign]

    with pytest.raises(SkillVersionDeleteBlockedError):
        await repository.delete_skill_version(
            organization_id=organization_id,
            skill_version_id=aggregate[1].id,
            authorize=lambda: datetime.now(UTC),
        )
    session.delete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version_status", [SkillVersionStatus.DRAFT, SkillVersionStatus.DEPRECATED]
)
async def test_delete_skill_version_removes_manifest_and_project_visibility(
    version_status: SkillVersionStatus,
) -> None:
    """参照のない草稿/廃止版は Manifest と Project 可視性設定ごと取り除く。"""

    organization_id = uuid4()
    skill, version, manifest = _version_aggregate(
        organization_id, status=version_status
    )
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=0)
    session.execute = AsyncMock()
    session.delete = AsyncMock()
    repository = SkillRepository(session)
    repository._version_models = AsyncMock(  # type: ignore[method-assign]
        return_value=(skill, version, manifest)
    )

    await repository.delete_skill_version(
        organization_id=organization_id,
        skill_version_id=version.id,
        authorize=lambda: datetime.now(UTC),
    )

    # 可視性 row の一括削除と、Manifest → Version の順の物理削除を確認する。
    assert session.scalar.await_count == 6
    assert session.execute.await_count == 1
    assert [call.args[0] for call in session.delete.await_args_list] == [manifest, version]
