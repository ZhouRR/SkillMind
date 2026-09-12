"""SkillService の parse-to-persistence と model interpret use case を検証する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

import pytest
from skillmind.db.models import SkillInterpretation, SkillSource
from skillmind.skills import (
    InlineSkillFile,
    SkillInterpretationNotReadyError,
    SkillInterpretationStatus,
    SkillInterpreterUnavailableError,
    SkillPackageParser,
    SkillService,
    TaskInputInvalidError,
    load_capability_catalog,
    load_inline_text_files,
    load_interpreter_system_skill,
)
from skillmind.skills.interpreter_execution import (
    InterpreterErrorCode,
    InterpreterExecutionError,
)
from tests.skills.interpreter_fakes import FixtureSkillInterpreter
from tests.skills.skill_import_authorization_harness import ImportSession

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
TEST_SKILLS = Path(__file__).resolve().parents[1] / "fixtures/skills"
GENERIC_SKILL = TEST_SKILLS / "repository-review"
UNSAFE_CREDENTIAL = TEST_SKILLS / "unsafe-credential"
SYSTEM_SKILL = ROOT / "skills" / "skillmind-skill-interpreter"
CATALOG = CONTRACTS / "examples" / "skill-capability-catalog.v1.json"


class _EmptyResult:
    """Repository lookup に未登録を返す scalar result。"""

    def one_or_none(self) -> None:
        """既存 model がないことを返す。"""

        return None


class _Transaction:
    """Test session の async transaction context。"""

    async def __aenter__(self) -> Self:
        """Transaction context を開始する。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """Exception を抑制せず transaction context を終了する。"""


class _Session:
    """SkillRepository が利用する最小 AsyncSession seam。"""

    def __init__(self) -> None:
        """追加 model を検査できるよう保持する。"""

        self.added: list[object] = []

    async def __aenter__(self) -> Self:
        """Session context を開始する。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """Exception を抑制せず Session context を終了する。"""

    def begin(self) -> _Transaction:
        """Service が所有する transaction context を返す。"""

        return _Transaction()

    async def scalars(self, statement: object) -> _EmptyResult:
        """Source/interpretation lookup を未登録として扱う。"""

        del statement
        return _EmptyResult()

    def add(self, model: object) -> None:
        """Repository が追加した model を記録する。"""

        self.added.append(model)


class _SessionFactory:
    """同じ test session を返す session factory。"""

    def __init__(self, session: _Session) -> None:
        """Use case 後に model を確認する session を保持する。"""

        self._session = session

    def __call__(self) -> _Session:
        """Async context 対応 session を返す。"""

        return self._session


@pytest.mark.asyncio
async def test_save_inline_parses_and_persists_deterministic_preview() -> None:
    """Parser 出力の hash、version、diagnostic が永続 model へ渡される。"""

    session = ImportSession()
    service = session.service()

    stored = await service.save_inline(
        access=session.access,
        files=(
            InlineSkillFile(
                path="SKILL.md",
                content="---\nname: Stored Skill\nallowed-tools: [Read]\n---\n# Stored Skill\n",
            ),
        ),
    )

    source = next(model for model in session.added if isinstance(model, SkillSource))
    interpretation = next(
        model for model in session.added if isinstance(model, SkillInterpretation)
    )
    assert source.name == "Stored Skill"
    assert source.content_hash.startswith("sha256:")
    assert interpretation.interpreter_version == "deterministic-parser/1.0.0"
    assert interpretation.compatibility_level == "assisted"
    assert interpretation.model is None
    assert stored.interpretation_id == interpretation.id


class _RaisingInterpreter:
    """呼ばれたら失敗する、idempotent 復用を検証するための interpreter。"""

    async def interpret(
        self,
        request: object,
        *,
        model: str,
        parameters: object,
        validation_feedback: str | None = None,
        on_event: object = None,
    ) -> dict[str, Any]:
        """Model が呼ばれないことを保証するため常に失敗する。"""

        del request, model, parameters, validation_feedback, on_event
        raise AssertionError("interpreter must not be called on idempotent reuse")


class _RepairingInterpreter:
    """最初は不正、validation feedback 後は正しい response を返す interpreter。"""

    def __init__(self) -> None:
        """呼び出しごとの feedback を監査用に保持する。"""

        self.feedback: list[str | None] = []

    async def interpret(
        self,
        request: Mapping[str, Any],
        *,
        model: str,
        parameters: Mapping[str, Any],
        validation_feedback: str | None = None,
        on_event: object = None,
    ) -> dict[str, Any]:
        """二回目だけ fixture response を返し、受控修復経路を再現する。"""

        del request, model, parameters, on_event
        self.feedback.append(validation_feedback)
        return {"response_version": "wrong"} if validation_feedback is None else _example_response()


class _DecodeRepairingInterpreter:
    """最初の decode 失敗後、脱敏 feedback を受けて完全 response を返す。"""

    def __init__(self, code: InterpreterErrorCode) -> None:
        """最初に送出する安定 error code を保持する。"""

        self.code = code
        self.feedback: list[str | None] = []

    async def interpret(
        self,
        request: Mapping[str, Any],
        *,
        model: str,
        parameters: Mapping[str, Any],
        validation_feedback: str | None = None,
        on_event: object = None,
    ) -> dict[str, Any]:
        """Feedback のない初回だけ分類済み失敗を送出する。"""

        del request, model, parameters, on_event
        self.feedback.append(validation_feedback)
        if validation_feedback is None:
            raise InterpreterExecutionError(self.code)
        return _example_response()


class _InterpretResult:
    """設定した value を one_or_none で返す scalar result。"""

    def __init__(self, value: object) -> None:
        """Reuse 検索の戻り値を保持する。"""

        self._value = value

    def one_or_none(self) -> object:
        """既存 model interpretation 行または未登録を返す。"""

        return self._value


class _InterpretSession:
    """get と順序付き scalars を提供する interpret 用 session seam。"""

    def __init__(self, source: SkillSource, scalars_results: list[object]) -> None:
        """全 get が返す source と、scalars 呼び出しの順序結果を保持する。"""

        self._source = source
        self._scalars_results = scalars_results
        self.added: list[object] = []

    async def __aenter__(self) -> Self:
        """Session context を開始する。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """Exception を抑制せず Session context を終了する。"""

    def begin(self) -> _Transaction:
        """Service が所有する transaction context を返す。"""

        return _Transaction()

    async def get(self, model: object, identifier: object) -> SkillSource:
        """SkillSource lookup を常に同じ source として解決する。"""

        del model, identifier
        return self._source

    async def scalars(self, statement: object) -> _InterpretResult:
        """Reuse 検索を設定順に解決する。"""

        del statement
        return _InterpretResult(self._scalars_results.pop(0))

    def add(self, model: object) -> None:
        """Repository が追加した model を記録する。"""

        self.added.append(model)


class _InterpretFactory:
    """同じ interpret session を返す session factory。"""

    def __init__(self, session: _InterpretSession | _AdjustSession) -> None:
        """追加 model を検査する session を保持する。"""

        self._session = session

    def __call__(self) -> _InterpretSession | _AdjustSession:
        """Async context 対応 session を返す。"""

        return self._session


def _example_response() -> dict[str, Any]:
    """S2 model 経路用に compiler 出力を含まない response fixture を組み立てる。"""

    value = json.loads(
        (CONTRACTS / "examples" / "skill-interpreter-response.v1.json").read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    generated = json.loads(
        (CONTRACTS / "examples" / "generated-task-manifest.v1alpha1.json").read_text(
            encoding="utf-8"
        )
    )
    task = generated["tasks"][0]
    for key in (
        "input_schema",
        "output_schema",
        "input_schema_checksum",
        "output_schema_checksum",
    ):
        task.pop(key)
    value["runtime_manifest_draft"]["tasks"] = [task]
    return value


def _fixture_source(root: Path, organization_id: object, source_id: object) -> SkillSource:
    """Fixture directory を再解析可能な永続 source snapshot へ変換する。"""

    package = SkillPackageParser().parse_directory(root.resolve())
    snapshot = [
        {"path": item.path, "content": item.content}
        for item in load_inline_text_files(root, package)
    ]
    return SkillSource(
        id=source_id,
        organization_id=organization_id,
        name="Repository Reviewer",
        source_type="directory",
        storage_uri="database://skill-sources/fixture",
        content_hash=package.content_hash,
        source_version=None,
        imported_by=uuid4(),
        source_snapshot_json=snapshot,
        created_at=datetime(2026, 7, 8, tzinfo=UTC),
    )


def _service(session: _InterpretSession | _AdjustSession, interpreter: object) -> SkillService:
    """Frozen catalog と system Skill identity を束ねた interpret 対応 service を作る。"""

    return SkillService(
        _InterpretFactory(session),  # type: ignore[arg-type]
        CONTRACTS,
        interpreter=interpreter,  # type: ignore[arg-type]
        capability_catalog=load_capability_catalog(CATALOG),
        interpreter_identity=load_interpreter_system_skill(SYSTEM_SKILL),
    )


@pytest.mark.asyncio
async def test_interpret_persists_preview_ready_model_interpretation() -> None:
    """検証済み model 応答を PREVIEW_READY の不変 record として保存する。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    session = _InterpretSession(source, scalars_results=[None, None])
    service = _service(session, FixtureSkillInterpreter(_example_response()))

    stored = await service.interpret(
        organization_id=organization_id, skill_source_id=source_id, model="claude-opus-4-8"
    )

    interpretation = next(
        model for model in session.added if isinstance(model, SkillInterpretation)
    )
    assert stored.status is SkillInterpretationStatus.PREVIEW_READY
    assert stored.reused is False
    assert stored.error_code is None
    assert stored.model == "claude-opus-4-8"
    assert interpretation.origin == "model"
    task = interpretation.manifest_draft_json["tasks"][0]
    assert task["input_schema_checksum"] == (
        "sha256:80d83714d6fd98b37626acec9bfe7c9ba8dc749812549f523c01a02e1281f75c"
    )
    assert task["output_schema"]["additionalProperties"] is False
    assert interpretation.report_json == _example_response()["report"]
    assert interpretation.compatibility_level == "adapted"
    assert interpretation.confidence == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_interpret_reuses_same_frozen_identity_without_model_call() -> None:
    """同じ frozen identity の再要求は既存 record を返し、model を再実行しない。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    first_session = _InterpretSession(source, scalars_results=[None, None])
    first = await _service(first_session, FixtureSkillInterpreter(_example_response())).interpret(
        organization_id=organization_id, skill_source_id=source_id, model="claude-opus-4-8"
    )
    row = next(model for model in first_session.added if isinstance(model, SkillInterpretation))

    reused_session = _InterpretSession(source, scalars_results=[row])
    reused = await _service(reused_session, _RaisingInterpreter()).interpret(
        organization_id=organization_id, skill_source_id=source_id, model="claude-opus-4-8"
    )

    assert reused.reused is True
    assert reused.execution_key == first.execution_key
    assert reused_session.added == []


@pytest.mark.asyncio
async def test_interpret_force_regenerate_creates_a_new_execution_key() -> None:
    """明示 regeneration は既存 identity を変えず、一回限りの nonce で別 execution を作る。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    keys: list[str] = []
    for _ in range(2):
        session = _InterpretSession(source, scalars_results=[None])
        stored = await _service(session, FixtureSkillInterpreter(_example_response())).interpret(
            organization_id=organization_id,
            skill_source_id=source_id,
            model="claude-opus-4-8",
            force_regenerate=True,
        )
        assert stored.execution_key is not None
        keys.append(stored.execution_key)

    assert keys[0] != keys[1]


@pytest.mark.asyncio
async def test_interpret_records_schema_failure_without_publishable_manifest() -> None:
    """契約に反する model 応答を FAILED として記録し、Manifest を空にする。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    session = _InterpretSession(source, scalars_results=[None, None])
    service = _service(session, FixtureSkillInterpreter({"response_version": "wrong"}))

    stored = await service.interpret(
        organization_id=organization_id, skill_source_id=source_id, model="claude-opus-4-8"
    )

    interpretation = next(
        model for model in session.added if isinstance(model, SkillInterpretation)
    )
    assert stored.status is SkillInterpretationStatus.FAILED
    assert stored.error_code == "schema_validation_failed"
    assert interpretation.manifest_draft_json == {}
    assert interpretation.report_json is None
    # 契約違反の内訳が脱敏済みで監査 record に残る (単一 code だけの盲目失敗にしない)。
    assert interpretation.execution_json is not None
    detail = interpretation.execution_json["detail"]
    assert isinstance(detail, str) and detail
    assert len(interpretation.execution_json["validation_attempts"]) == 2
    assert stored.validation_attempts == ("/: required", "/: required")
    assert all("wrong" not in attempt for attempt in stored.validation_attempts)


@pytest.mark.asyncio
async def test_interpret_repairs_one_schema_failure_with_sanitized_feedback() -> None:
    """最初の Schema 違反だけを一度再生成し、失敗内訳を成功 record に監査保存する。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    session = _InterpretSession(source, scalars_results=[None, None])
    interpreter = _RepairingInterpreter()

    stored = await _service(session, interpreter).interpret(
        organization_id=organization_id,
        skill_source_id=source_id,
        model="claude-opus-4-8",
    )

    row = next(model for model in session.added if isinstance(model, SkillInterpretation))
    assert stored.status is SkillInterpretationStatus.PREVIEW_READY
    assert interpreter.feedback[0] is None
    assert interpreter.feedback[1]
    assert row.execution_json is not None
    assert row.execution_json["validation_attempts"] == [interpreter.feedback[1]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        InterpreterErrorCode.EMPTY_RESPONSE,
        InterpreterErrorCode.INVALID_JSON,
        InterpreterErrorCode.TRUNCATED_OUTPUT,
    ],
)
async def test_interpret_repairs_one_candidate_decode_failure(
    code: InterpreterErrorCode,
) -> None:
    """不完全候補だけを一度再生成し、本文を含まない分類を監査へ残す。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    session = _InterpretSession(source, scalars_results=[None, None])
    interpreter = _DecodeRepairingInterpreter(code)

    stored = await _service(session, interpreter).interpret(
        organization_id=organization_id,
        skill_source_id=source_id,
        model="claude-opus-4-8",
    )

    row = next(model for model in session.added if isinstance(model, SkillInterpretation))
    assert stored.status is SkillInterpretationStatus.PREVIEW_READY
    assert interpreter.feedback == [None, f"candidate_generation:{code.value}"]
    assert row.execution_json is not None
    assert row.execution_json["validation_attempts"] == [interpreter.feedback[1]]


@pytest.mark.asyncio
async def test_interpret_does_not_retry_provider_failure() -> None:
    """Upstream/provider 障害を候補修復で覆い隠さず、元の分類で即時失敗する。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    session = _InterpretSession(source, scalars_results=[None, None])
    interpreter = _DecodeRepairingInterpreter(InterpreterErrorCode.PROVIDER_ERROR)

    stored = await _service(session, interpreter).interpret(
        organization_id=organization_id,
        skill_source_id=source_id,
        model="claude-opus-4-8",
    )

    assert stored.status is SkillInterpretationStatus.FAILED
    assert stored.error_code == InterpreterErrorCode.PROVIDER_ERROR.value
    assert interpreter.feedback == [None]


@pytest.mark.asyncio
async def test_interpret_blocks_unsafe_source_before_model_call() -> None:
    """危険 source を model 呼び出し前に UNSAFE_SOURCE として記録する。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(UNSAFE_CREDENTIAL, organization_id, source_id)
    session = _InterpretSession(source, scalars_results=[None])
    service = _service(session, _RaisingInterpreter())

    stored = await service.interpret(
        organization_id=organization_id, skill_source_id=source_id, model="claude-opus-4-8"
    )

    assert stored.status is SkillInterpretationStatus.FAILED
    assert stored.error_code == "unsafe_source"
    assert session.added and isinstance(session.added[0], SkillInterpretation)


@pytest.mark.asyncio
async def test_interpret_requires_configured_interpreter() -> None:
    """Interpreter 未配線の service は interpret 要求を明示的に拒否する。"""

    service = SkillService(_SessionFactory(_Session()), ROOT / "contracts")  # type: ignore[arg-type]

    with pytest.raises(SkillInterpreterUnavailableError):
        await service.interpret(
            organization_id=uuid4(), skill_source_id=uuid4(), model="claude-opus-4-8"
        )


class _AdjustSession:
    """get を model 種別で解決し、reinterpretation の lineage を検証する session。"""

    def __init__(
        self, parent: SkillInterpretation, source: SkillSource, scalars_results: list[object]
    ) -> None:
        """親 interpretation、source、scalars の順序結果を保持する。"""

        self._parent = parent
        self._source = source
        self._scalars_results = scalars_results
        self.added: list[object] = []

    async def __aenter__(self) -> Self:
        """Session context を開始する。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """Exception を抑制せず Session context を終了する。"""

    def begin(self) -> _Transaction:
        """Service が所有する transaction context を返す。"""

        return _Transaction()

    async def get(self, model: object, identifier: object) -> object:
        """SkillInterpretation は親を、その他は source を返す。"""

        if model is SkillInterpretation:
            return self._parent if identifier == self._parent.id else None
        return self._source

    async def scalars(self, statement: object) -> _InterpretResult:
        """Reuse 検索を設定順に解決する。"""

        del statement
        return _InterpretResult(self._scalars_results.pop(0))

    def add(self, model: object) -> None:
        """Repository が追加した model を記録する。"""

        self.added.append(model)


def _model_interpretation_row(
    *,
    source_id: object,
    status: SkillInterpretationStatus,
    manifest: dict[str, Any],
    report: dict[str, Any] | None,
    checksum: str,
) -> SkillInterpretation:
    """永続 model interpretation 行を生成する。"""

    return SkillInterpretation(
        id=uuid4(),
        skill_source_id=source_id,
        origin="model",
        interpreter_version="skillmind-skill-interpreter/1.0.0",
        model="claude-opus-4-8",
        compatibility_level="adapted",
        status=status.value,
        summary="Parent execution",
        confidence=0.85,
        assumptions_json=[],
        questions_json=[],
        diagnostics_json=[],
        normalized_package_json={},
        manifest_draft_json=manifest,
        report_json=report,
        execution_json={"error_code": None},
        checksum=checksum,
        created_at=datetime(2026, 7, 8, tzinfo=UTC),
        parent_interpretation_id=None,
        adjustment_json=None,
    )


@pytest.mark.asyncio
async def test_adjust_creates_child_with_lineage_and_diff() -> None:
    """調整指示が親を保ったまま lineage と構造化 diff 付きの子を生成する。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    parent_manifest = deepcopy(_example_response()["runtime_manifest_draft"])
    parent_manifest["tasks"] = []
    parent = _model_interpretation_row(
        source_id=source_id,
        status=SkillInterpretationStatus.PREVIEW_READY,
        manifest=parent_manifest,
        report=_example_response()["report"],
        checksum="sha256:" + ("a" * 64),
    )
    session = _AdjustSession(parent, source, scalars_results=[None, None])
    service = _service(session, FixtureSkillInterpreter(_example_response()))

    stored = await service.adjust_interpretation(
        organization_id=organization_id,
        interpretation_id=parent.id,
        instruction="Focus the review on a single file.",
        actor_id=uuid4(),
        model="claude-opus-4-8",
    )

    child = next(model for model in session.added if isinstance(model, SkillInterpretation))
    assert child.parent_interpretation_id == parent.id
    assert child.adjustment_json is not None
    assert child.adjustment_json["instruction"] == "Focus the review on a single file."
    assert child.checksum != parent.checksum
    assert stored.parent_interpretation_id == parent.id
    assert stored.diff["tasks"]["added"] == ["review-file"]
    assert stored.diff["has_changes"] is True
    # 親は書き換えられない: append-only を守る。
    assert parent.status == SkillInterpretationStatus.PREVIEW_READY.value
    assert parent.manifest_draft_json["tasks"] == []


@pytest.mark.asyncio
async def test_get_interpretation_execution_without_parent_has_empty_diff() -> None:
    """親を持たない実行 detail は空の diff で返る。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    row = _model_interpretation_row(
        source_id=source_id,
        status=SkillInterpretationStatus.PREVIEW_READY,
        manifest=_example_response()["runtime_manifest_draft"],
        report=_example_response()["report"],
        checksum="sha256:" + ("b" * 64),
    )
    session = _AdjustSession(row, source, scalars_results=[])
    service = _service(session, FixtureSkillInterpreter(_example_response()))

    stored = await service.get_interpretation_execution(
        organization_id=organization_id, interpretation_id=row.id
    )

    assert stored.diff == {}
    assert stored.parent_interpretation_id is None
    assert stored.report == _example_response()["report"]


@pytest.mark.asyncio
async def test_adjust_rejects_non_preview_ready_parent() -> None:
    """FAILED な親への調整は model を呼ばず拒否する。"""

    organization_id = uuid4()
    source_id = uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    parent = _model_interpretation_row(
        source_id=source_id,
        status=SkillInterpretationStatus.FAILED,
        manifest={},
        report=None,
        checksum="sha256:" + ("c" * 64),
    )
    session = _AdjustSession(parent, source, scalars_results=[])
    service = _service(session, FixtureSkillInterpreter(_example_response()))

    with pytest.raises(SkillInterpretationNotReadyError):
        await service.adjust_interpretation(
            organization_id=organization_id,
            interpretation_id=parent.id,
            instruction="anything",
            actor_id=uuid4(),
            model="claude-opus-4-8",
        )
    assert session.added == []


def _input_service() -> SkillService:
    """Session を使わない input 検証用の SkillService を作る。"""

    return SkillService(_SessionFactory(_Session()), CONTRACTS)  # type: ignore[arg-type]


def test_validate_task_input_accepts_schema_valid_input() -> None:
    """Generated Schema に適合する入力は例外を投げない。"""

    service = _input_service()
    schema = {
        "type": "object",
        "required": ["target_path"],
        "properties": {"target_path": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    }
    service._validate_task_input(
        schema,
        {"target_path": "src/example.py"},
    )


def test_validate_task_input_rejects_schema_invalid_input() -> None:
    """Generated Schema の必須欠落は TaskInputInvalidError を送出する。"""

    service = _input_service()
    schema = {
        "type": "object",
        "required": ["target_path"],
        "properties": {"target_path": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    }
    with pytest.raises(TaskInputInvalidError):
        service._validate_task_input(schema, {})
