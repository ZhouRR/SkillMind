"""実 Service/Repository/validator を通し、保存済み発行 aggregate の拒否境界を検査する。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import Select
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.api.problems import ProblemException, problem_exception_handler
from projectmind.api.routes.skills import PublishSkillVersionRequest, publish_skill_version
from projectmind.auth.domain import generate_session_credentials
from projectmind.auth.service import AuthenticatedActor
from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.core.settings import Settings
from projectmind.db.models import (
    AuthSession,
    Organization,
    RuntimeManifest,
    Skill,
    SkillInterpretation,
    SkillSource,
    SkillVersion,
    User,
)
from projectmind.skills.design_validation import SkillDesignSource
from projectmind.skills.domain import (
    ManifestGateFinding,
    SkillPublishGateError,
    SkillVersionNotFoundError,
    SkillVersionStatus,
    StoredSkillVersion,
)
from projectmind.skills.manifest_gate import ManifestValidator
from projectmind.skills.service import SkillService
from projectmind.users.domain import UserAccess
from tests.skills.task_flow_fixtures import make_task_flow_source

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
Model = (
    Skill
    | SkillVersion
    | RuntimeManifest
    | SkillSource
    | SkillInterpretation
    | Organization
    | User
    | AuthSession
)


class PublicationTransaction:
    """実 DB の rollback を模倣せず、拒否前に model を変更しないことを観測する。"""

    def __init__(self, session: PublicationSession) -> None:
        """Transaction の成功/失敗を session の履歴に保存する。"""
        self.session = session

    async def __aenter__(self) -> Self:
        """Service が要求した transaction 開始を記録する。"""
        self.session.events.append("begin")
        return self

    async def __aexit__(self, kind: object, error: object, traceback: object) -> None:
        """Exception を抑制せず終了を記録する。"""
        self.session.events.append("commit" if kind is None else "rollback")


class PublicationResult:
    """精確な保存 aggregate 検索の結果だけを返す。"""

    def __init__(
        self, value: Model | str | None, *, values: tuple[Model, ...] | None = None
    ) -> None:
        """SQL の帰属条件に合う一行または欠損を保持する。"""
        self.value = value
        self.values = values

    def one_or_none(self) -> Model | str | None:
        """他版への fallback をせず元の検索結果を返す。"""
        return self.value

    def all(self) -> list[Model | str]:
        """版番号列挙も同じ一行 fixture の範囲だけ返す。"""
        if self.values is not None:
            return list(self.values)
        return [] if self.value is None else [self.value]


class PublicationSession:
    """永続 ORM aggregate と実 SELECT を用いる offline session seam。"""

    def __init__(self) -> None:
        """原 source index/hash/解釈/二つの Task を持つ合法候補を作る。"""
        source = make_task_flow_source()
        now = datetime(2026, 9, 10, tzinfo=UTC)
        self.organization_id = uuid4()
        self.publisher = uuid4()
        self.organization: Organization | None = Organization(
            id=self.organization_id, name="Synthetic organization"
        )
        self.user = User(
            id=self.publisher,
            organization_id=self.organization_id,
            email="publisher@example.test",
            display_name="Synthetic publisher",
            system_role="ADMIN",
            status="ACTIVE",
        )
        credentials = generate_session_credentials()
        auth_now = datetime.now(UTC)
        self.auth_session = AuthSession(
            id=uuid4(),
            user_id=self.publisher,
            token_hash=credentials.session_token_hash,
            csrf_token_hash=credentials.csrf_token_hash,
            credential_version=2,
            system_role_at_login="ADMIN",
            revoked_at=None,
            created_at=auth_now,
            last_seen_at=auth_now,
            idle_expires_at=auth_now + timedelta(minutes=30),
            absolute_expires_at=auth_now + timedelta(hours=8),
        )
        self.users = [self.user]
        self.auth_sessions = [self.auth_session]
        self.access = UserAccess(
            actor=AuthenticatedActor(
                self.publisher,
                self.organization_id,
                self.user.email,
                self.user.display_name,
                "ADMIN",
            ),
            request_id=uuid4(),
            session_token=credentials.session_token,
            csrf_token=credentials.csrf_token,
        )
        self.skill = Skill(
            id=source.skill_id,
            organization_id=self.organization_id,
            key=source.skill_key,
            name="Synthetic publication",
            description="Original description",
            status="DRAFT",
            created_at=now,
            updated_at=now,
        )
        self.version = SkillVersion(
            id=source.skill_version_id,
            skill_id=source.skill_id,
            skill_source_id=source.skill_source_id,
            interpretation_id=source.interpretation_id,
            status="DRAFT",
            version=source.version,
            gate_report_json={
                "passed": True,
                "findings": [],
                "interpretation_diff": {},
                "accepted_warnings": [],
            },
            published_by=None,
            published_at=None,
            created_at=now,
            updated_at=now,
        )
        self.manifest = RuntimeManifest(
            id=uuid4(),
            skill_version_id=source.skill_version_id,
            interpretation_id=source.interpretation_id,
            manifest_version=source.manifest["manifest_version"],
            manifest_json=deepcopy(source.manifest),
            checksum=source.manifest_checksum,
            created_at=now,
        )
        self.source = SkillSource(
            id=source.skill_source_id,
            organization_id=self.organization_id,
            name="Synthetic publication",
            source_type="directory",
            storage_uri="database://skill-sources/synthetic",
            content_hash=source.source_hash,
            source_version=None,
            imported_by=self.publisher,
            source_file_index_json=deepcopy(source.source_file_index),
            source_snapshot_json=deepcopy(source.source_snapshot),
            created_at=now,
        )
        self.interpretation = SkillInterpretation(
            id=source.interpretation_id,
            skill_source_id=source.skill_source_id,
            origin="model",
            status="PREVIEW_READY",
            interpreter_version=source.interpreter_version,
            model="synthetic-model",
            compatibility_level="adapted",
            summary="Original interpretation",
            confidence=0.8,
            assumptions_json=[],
            questions_json=[],
            diagnostics_json=[],
            normalized_package_json={},
            manifest_draft_json=deepcopy(source.manifest),
            report_json=None,
            execution_json=None,
            parent_interpretation_id=None,
            adjustment_json=None,
            checksum="sha256:" + "e" * 64,
            created_at=now,
        )
        self.rows: tuple[Model, ...] = (
            self.skill,
            self.version,
            self.manifest,
            self.source,
            self.interpretation,
        )
        self.missing: set[type[Model]] = set()
        self.events: list[str] = []
        self.queries: list[str] = []
        self.loads: list[tuple[object, object, dict[str, object]]] = []
        self.flush_calls = 0

    async def __aenter__(self) -> Self:
        """実 Service が session を開く境界を提供する。"""
        return self

    async def __aexit__(self, *args: object) -> None:
        """実接続を持たない session の終了を記録する。"""
        self.events.append("close")

    def begin(self) -> PublicationTransaction:
        """Service 所有の transaction を観測する。"""
        return PublicationTransaction(self)

    async def get(self, model: object, identity: object, **options: object) -> Model | None:
        """主キー一致行だけを返し、FK 欠損を補造しない。"""
        self.loads.append((model, identity, options))
        return next(
            (
                row
                for row in self.rows
                if type(row) is model and row.id == identity and type(row) not in self.missing
            ),
            None,
        )

    async def scalar(self, statement: Select[tuple[Any, ...]]) -> Model | None:
        """共有 Organization lock の SQL を検査し、認証を固定成功へ置換しない。"""
        sql = str(statement)
        self.queries.append(sql)
        assert statement.column_descriptions[0]["entity"] is Organization
        assert "organizations.id =" in sql and "FOR UPDATE" in sql
        parameters = statement.compile().params
        assert set(parameters) == {"id_1"}
        return (
            self.organization
            if self.organization is not None and self.organization.id == parameters["id_1"]
            else None
        )

    async def scalars(self, statement: Select[tuple[Any, ...]]) -> PublicationResult:
        """本番 SQL の Manifest scope を検査し、同じ WHERE 条件を適用する。"""
        compiled = statement.compile(dialect=cast(Callable[[], Dialect], dialect)())
        sql = str(compiled)
        self.queries.append(sql)
        model = statement.column_descriptions[0]["entity"]
        if model is User:
            assert "users.organization_id =" in sql and "users.id IN" in sql
            assert "ORDER BY users.id" in sql and "FOR SHARE" in sql
            assert statement.get_execution_options()["populate_existing"] is True
            assert set(compiled.params) == {"organization_id_1", "id_1"}
            organization_id = compiled.params["organization_id_1"]
            identities = compiled.params["id_1"]
            return PublicationResult(
                None,
                values=tuple(
                    user
                    for user in self.users
                    if user.organization_id == organization_id and user.id in identities
                ),
            )
        if model is AuthSession:
            assert "auth_sessions.user_id =" in sql and "auth_sessions.token_hash =" in sql
            assert "ORDER BY auth_sessions.id" in sql and "FOR UPDATE" in sql
            assert statement.get_execution_options()["populate_existing"] is True
            assert set(compiled.params) == {"user_id_1", "token_hash_1"}
            return PublicationResult(
                None,
                values=tuple(
                    current
                    for current in self.auth_sessions
                    if current.user_id == compiled.params["user_id_1"]
                    and current.token_hash == compiled.params["token_hash_1"]
                ),
            )
        assert "runtime_manifests.skill_version_id =" in sql
        assert set(compiled.params.values()) == {self.version.id}
        found = (
            RuntimeManifest not in self.missing
            and self.manifest.skill_version_id == self.version.id
        )
        return PublicationResult(self.manifest if found else None)

    async def flush(self) -> None:
        """実 service の最終 flush を数え、commit 成功と混同しない。"""
        self.flush_calls += 1

    def service(self) -> SkillService:
        """実 Service/Repository/validator は差し替えず session factory だけ接続する。"""
        return SkillService(cast(async_sessionmaker[AsyncSession], lambda: self), CONTRACTS)

    async def publish(self, accepted_warnings: frozenset[str] = frozenset()) -> StoredSkillVersion:
        """原版と ADMIN publisher に対する本番 use case を呼ぶ。"""
        return await self.service().publish_skill_version(
            access=self.access,
            skill_version_id=self.version.id,
            accepted_warnings=accepted_warnings,
        )

    def rehash(self) -> None:
        """意図的な semantic 反例では原 JSON checksum だけを正しく再計算する。"""
        self.manifest.checksum = "sha256:" + sha256_hex(canonical_json(self.manifest.manifest_json))

    def frozen_values(self) -> dict[str, object]:
        """発行拒否/再送の前後で全保存列が不変であることを比較する。"""
        return deepcopy(
            {
                type(row).__name__: {
                    column.key: getattr(row, column.key) for column in row.__table__.columns
                }
                for row in self.rows
            }
        )


@pytest.mark.asyncio
async def test_valid_saved_aggregate_publishes_without_changing_manifest() -> None:
    """二つの Task と原 source を実検証し、metadata だけ発行する。"""
    session = PublicationSession()
    manifest = deepcopy(session.manifest.manifest_json)
    checksum = session.manifest.checksum
    stored = await session.publish()
    assert stored.status is SkillVersionStatus.PUBLISHED
    assert stored.published_by == session.publisher
    assert stored.published_at is not None
    assert session.manifest.manifest_json == manifest
    assert session.manifest.checksum == checksum
    assert session.events == ["begin", "commit", "close"]


@pytest.mark.asyncio
async def test_real_validation_runs_under_original_version_lock_before_metadata_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実 validator の前後を観測し、lock なしの検査や先行発行を許さない。"""
    session = PublicationSession()
    before = session.frozen_values()
    original = ManifestValidator.evaluate
    calls: list[SkillDesignSource] = []

    def observe(
        validator: ManifestValidator,
        source: SkillDesignSource,
    ) -> tuple[bool, tuple[ManifestGateFinding, ...]]:
        """候補を偽の判定に置換せず、必ず本番 validator の判断を返す。"""
        assert session.events == ["begin"]
        assert session.frozen_values() == before
        model, identity, options = session.loads[0]
        assert model is SkillVersion and identity == session.version.id
        assert options.get("with_for_update") is True
        assert options.get("populate_existing") is True
        assert source.manifest == session.manifest.manifest_json
        assert source.manifest is not session.manifest.manifest_json
        assert source.source_file_index == session.source.source_file_index_json
        assert source.source_snapshot == session.source.source_snapshot_json
        calls.append(source)
        return original(validator, source)

    monkeypatch.setattr(ManifestValidator, "evaluate", observe)
    result = await session.publish()
    assert result.status is SkillVersionStatus.PUBLISHED
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,error",
    [
        (SkillVersion, SkillVersionNotFoundError),
        (SkillSource, SkillVersionNotFoundError),
        (SkillInterpretation, SkillPublishGateError),
        (RuntimeManifest, SkillPublishGateError),
    ],
)
async def test_missing_aggregate_member_never_publishes(
    model: type[Model],
    error: type[Exception],
) -> None:
    """不可視の source と、可視版内部の欠損を既存の異なる公開拒否へ保つ。"""
    session = PublicationSession()
    session.missing.add(model)
    before = session.frozen_values()
    with pytest.raises(error):
        await session.publish()
    assert session.frozen_values() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report",
    [
        None,
        [],
        {},
        {"passed": False, "findings": []},
        {"findings": []},
        {"passed": 1, "findings": []},
        {"passed": "true", "findings": []},
        {"passed": True},
        {"passed": True, "findings": None},
        {"passed": True, "findings": {}},
        {"passed": True, "findings": [None]},
        {"passed": True, "findings": [{}]},
        {"passed": True, "findings": [{"severity": "warning", "message": "Original note"}]},
        {"passed": True, "findings": [{"code": "warn", "severity": "unknown", "message": "Note"}]},
        {"passed": True, "findings": [{"code": 1, "severity": "warning", "message": "Note"}]},
    ],
)
async def test_saved_gate_must_explicitly_pass_before_publication(report: Any) -> None:
    """保存時 hard gate の欠損/false を findings 空配列から成功に変換しない。"""
    session = PublicationSession()
    session.version.gate_report_json = report
    before = session.frozen_values()
    with pytest.raises(SkillPublishGateError):
        await session.publish()
    assert session.frozen_values() == before
    assert session.events == ["begin", "rollback", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("interpretation_diff", None),
        ("interpretation_diff", []),
        ("interpretation_diff", "broken"),
        ("accepted_warnings", None),
        ("accepted_warnings", {}),
        ("accepted_warnings", "review"),
        ("accepted_warnings", [1]),
        ("accepted_warnings", [True]),
    ],
)
async def test_damaged_saved_gate_metadata_is_rejected_before_any_mutation(
    field: str,
    value: Any,
) -> None:
    """表示用 metadata の復元失敗で、先行して発行状態だけ書き換えることを防ぐ。"""
    session = PublicationSession()
    session.version.gate_report_json[field] = value
    before = session.frozen_values()
    with pytest.raises(SkillPublishGateError):
        await session.publish()
    assert session.frozen_values() == before
    assert session.events == ["begin", "rollback", "close"]


@pytest.mark.asyncio
async def test_missing_optional_saved_gate_metadata_remains_compatible() -> None:
    """旧保存 gate の任意 metadata 省略を、損傷した明示値と区別する。"""
    session = PublicationSession()
    session.version.gate_report_json = {"passed": True, "findings": []}
    result = await session.publish()
    assert result.status is SkillVersionStatus.PUBLISHED
    assert result.interpretation_diff == {}


@pytest.mark.asyncio
async def test_saved_info_finding_is_preserved_without_warning_acceptance() -> None:
    """公開契約の info を未知 severity とせず、受諾不要の監査情報として残す。"""
    session = PublicationSession()
    note = {
        "code": "source_reviewed",
        "severity": "info",
        "message": "Source indexed",
        "path": None,
    }
    session.version.gate_report_json["findings"] = [note]
    result = await session.publish()
    assert result.status is SkillVersionStatus.PUBLISHED
    assert result.gate_findings == (
        ManifestGateFinding(
            code="source_reviewed", severity="info", message="Source indexed", path=None
        ),
    )
    assert session.version.gate_report_json["findings"] == [note]
    assert session.version.gate_report_json["accepted_warnings"] == []


@pytest.mark.asyncio
async def test_saved_warning_acceptance_does_not_authorize_current_publication() -> None:
    """保存 report の受諾記録を今回の ADMIN request の明示受諾へ格上げしない。"""
    session = PublicationSession()
    session.version.gate_report_json["accepted_warnings"] = ["review"]
    session.version.gate_report_json["findings"] = [
        {"code": "review", "severity": "warning", "message": "Original warning", "path": None},
    ]
    before = session.frozen_values()
    with pytest.raises(SkillPublishGateError):
        await session.publish()
    assert session.frozen_values() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "manifest-hash",
        "source-hash",
        "source-index",
        "source-text",
        "missing-text",
        "interpretation-source",
        "interpretation-version",
        "interpretation-status",
        "interpretation-origin",
        "manifest-interpretation",
        "manifest-version",
    ],
)
async def test_saved_association_and_original_bytes_are_rechecked(case: str) -> None:
    """保存 report が true でも原 FK/解釈 identity/hash/本文の破損は発行しない。"""
    session = PublicationSession()
    if case == "manifest-hash":
        session.manifest.checksum = "sha256:" + "a" * 64
    elif case == "source-hash":
        session.source.content_hash = "sha256:" + "b" * 64
    elif case == "source-index":
        session.source.source_file_index_json[0]["size"] += 1
    elif case == "source-text":
        session.source.source_snapshot_json[0]["content"] += "Private source marker"
    elif case == "missing-text":
        session.source.source_snapshot_json = []
    elif case == "interpretation-source":
        session.interpretation.skill_source_id = uuid4()
    elif case == "interpretation-version":
        session.interpretation.interpreter_version = "other-interpreter/9.9.9"
    elif case == "interpretation-status":
        session.interpretation.status = "FAILED"
    elif case == "interpretation-origin":
        session.interpretation.origin = "deterministic_parser"
    elif case == "manifest-interpretation":
        session.manifest.interpretation_id = uuid4()
    else:
        session.manifest.manifest_version = "projectmind/unknown"
    before = session.frozen_values()
    with pytest.raises(SkillPublishGateError):
        await session.publish()
    assert session.frozen_values() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [Skill, SkillSource])
async def test_foreign_organization_is_hidden_before_publication(
    model: type[Skill | SkillSource],
) -> None:
    """同じ内容でも別 Organization の保存資産を 404 相当へ隠す。"""
    session = PublicationSession()
    record = session.skill if model is Skill else session.source
    record.organization_id = uuid4()
    before = session.frozen_values()
    with pytest.raises(SkillVersionNotFoundError):
        await session.publish()
    assert session.frozen_values() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing-blueprint",
        "blueprint-identity",
        "manifest-identity",
        "missing-first-task",
        "other-task-capability",
        "other-task-contract",
        "other-task-contract-trace",
        "trace-missing-target",
        "trace-other-task-target",
        "trace-missing-file",
        "trace-line",
        "trace-path-escape",
        "trace-not-array",
        "duplicate-note",
        "null-shared",
        "required-resource",
    ],
)
async def test_complete_design_is_revalidated_despite_valid_manifest_checksum(case: str) -> None:
    """選択した一 Task だけではなく、全 Task/trace/共通宣言の破損を検出する。"""
    session = PublicationSession()
    manifest = session.manifest.manifest_json
    blueprint = manifest["capability_blueprint"]
    if case == "missing-blueprint":
        manifest["capability_blueprint"] = None
    elif case == "blueprint-identity":
        blueprint["identity"]["interpretation_id"] = str(uuid4())
    elif case == "manifest-identity":
        manifest["identity"]["skill_key"] = "foreign-skill"
    elif case == "missing-first-task":
        manifest["tasks"].pop(0)
    elif case == "other-task-capability":
        manifest["tasks"][0]["capability"] = "foreign.business"
    elif case == "other-task-contract":
        manifest["tasks"][0]["input_schema_checksum"] = "sha256:" + "f" * 64
    elif case == "other-task-contract-trace":
        manifest["tasks"][0]["contract_source_trace"][0]["source_path"] = "absent.md"
    elif case == "trace-missing-target":
        blueprint["source_traces"][0]["target"] = "/tasks/1/not_present"
    elif case == "trace-other-task-target":
        blueprint["source_traces"][-1]["target"] = "/tasks/0/not_present"
    elif case == "trace-missing-file":
        blueprint["source_traces"][0]["path"] = "absent.md"
    elif case == "trace-line":
        blueprint["source_traces"][0]["line"] = 100
    elif case == "trace-path-escape":
        blueprint["source_traces"][0]["path"] = "../private.md"
    elif case == "trace-not-array":
        blueprint["source_traces"] = {"target": "/tasks/0"}
    elif case == "duplicate-note":
        blueprint["guidance"]["required_rules"] *= 2
    elif case == "null-shared":
        blueprint["guidance"] = None
    else:
        blueprint["resource_requirements"][0]["required"] = "false"
    session.rehash()
    before = session.frozen_values()
    with pytest.raises(SkillPublishGateError):
        await session.publish()
    assert session.frozen_values() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accepted", [frozenset(), frozenset({"irrelevant"}), frozenset({"review"})]
)
async def test_saved_warning_requires_exact_explicit_acceptance(accepted: frozenset[str]) -> None:
    """保存された warning は現在再計算で消えても受諾を省略しない。"""
    session = PublicationSession()
    session.version.gate_report_json["findings"] = [
        {"code": "review", "severity": "warning", "message": "Review original", "path": None},
    ]
    before = session.frozen_values()
    if "review" in accepted:
        result = await session.publish(accepted)
        assert result.status is SkillVersionStatus.PUBLISHED
        assert session.version.gate_report_json["accepted_warnings"] == ["review"]
    else:
        with pytest.raises(SkillPublishGateError):
            await session.publish(accepted)
        assert session.frozen_values() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [frozenset(), frozenset({"assisted_review_required"})])
async def test_current_warning_cannot_be_omitted_by_saved_empty_findings(
    accepted: frozenset[str],
) -> None:
    """現 Manifest が assisted なら過去 report 空配列に関係なく明示受諾を要求する。"""
    session = PublicationSession()
    session.manifest.manifest_json["compatibility"]["level"] = "assisted"
    session.manifest.manifest_json["capability_blueprint"]["compatibility"]["level"] = "assisted"
    session.rehash()
    before = session.frozen_values()
    if accepted:
        result = await session.publish(accepted)
        assert result.status is SkillVersionStatus.PUBLISHED
    else:
        with pytest.raises(SkillPublishGateError):
            await session.publish()
        assert session.frozen_values() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accepted",
    [
        frozenset({"review"}),
        frozenset({"assisted_review_required"}),
        frozenset({"review", "assisted_review_required"}),
    ],
)
async def test_saved_and_current_warning_union_requires_both_acceptances(
    accepted: frozenset[str],
) -> None:
    """現在の再検査で保存 warning が消えても、どちらかの受諾だけでは発行しない。"""
    session = PublicationSession()
    session.version.gate_report_json["findings"] = [
        {"code": "review", "severity": "warning", "message": "Original warning", "path": None},
    ]
    session.manifest.manifest_json["compatibility"]["level"] = "assisted"
    session.manifest.manifest_json["capability_blueprint"]["compatibility"]["level"] = "assisted"
    session.rehash()
    before = session.frozen_values()
    if len(accepted) == 2:
        result = await session.publish(accepted)
        assert result.status is SkillVersionStatus.PUBLISHED
        assert session.version.gate_report_json["accepted_warnings"] == sorted(accepted)
    else:
        with pytest.raises(SkillPublishGateError):
            await session.publish(accepted)
        assert session.frozen_values() == before


@pytest.mark.asyncio
async def test_accepted_error_code_never_overrides_saved_hard_error() -> None:
    """ADMIN warning 受諾を保存 hard error の解除として利用しない。"""
    session = PublicationSession()
    session.version.gate_report_json["findings"] = [
        {"code": "blocked", "severity": "error", "message": "Original hard error", "path": None},
    ]
    before = session.frozen_values()
    with pytest.raises(SkillPublishGateError):
        await session.publish(frozenset({"blocked"}))
    assert session.frozen_values() == before


@pytest.mark.asyncio
async def test_published_replay_returns_original_metadata_without_regating_or_rewriting() -> None:
    """旧発行版を現門禁で書換えず、新 publisher/warning にも置換しない。"""
    session = PublicationSession()
    session.skill.status = "PUBLISHED"
    session.version.status = "PUBLISHED"
    session.version.published_at = session.version.created_at
    session.version.published_by = uuid4()
    session.version.gate_report_json = {
        "passed": False,
        "findings": [],
        "interpretation_diff": {},
        "accepted_warnings": ["old"],
    }
    session.manifest.manifest_json["capability_blueprint"] = None
    session.missing.add(SkillInterpretation)
    before = session.frozen_values()
    result = await session.publish(frozenset({"new"}))
    assert result.status is SkillVersionStatus.PUBLISHED
    assert result.published_by != session.publisher
    assert result.published_at == session.version.created_at
    assert session.frozen_values() == before


@pytest.mark.asyncio
async def test_repeated_publication_keeps_first_publisher_and_receipt() -> None:
    """同じ保存版への再送は新しい発行者/時刻/warning を記録しない。"""
    session = PublicationSession()
    first = await session.publish()
    frozen = session.frozen_values()
    session.publisher = uuid4()
    session.user.id = session.publisher
    session.auth_session.user_id = session.publisher
    session.access = replace(
        session.access, actor=replace(session.access.actor, user_id=session.publisher)
    )
    second = await session.publish(frozenset({"not-an-original-warning"}))
    assert second == first
    assert session.frozen_values() == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing-gate", "source-content", "warning"])
async def test_publish_route_maps_real_aggregate_rejection_to_existing_static_problem(
    case: str,
) -> None:
    """認証後 route/use case の実拒否を公開 409 へ変換し、保存本文を漏らさない。"""
    session = PublicationSession()
    marker = "Private source marker must never be a public diagnostic"
    if case == "missing-gate":
        session.version.gate_report_json = {}
    elif case == "source-content":
        session.source.source_snapshot_json[0]["content"] = marker
    else:
        session.version.gate_report_json["findings"] = [
            {"code": "review", "severity": "warning", "message": marker, "path": None},
        ]
    app = FastAPI()
    # この route seam は cookie 名だけを必要とし、BaseSettings の環境 source を読まない。
    app.state.settings = Settings.model_construct(
        auth_session_cookie_name="synthetic-skill-session"
    )
    app.state.skill_service = session.service()
    path = f"/api/v1/skill-versions/{session.version.id}/publish"
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": path,
            "headers": [
                (
                    b"cookie",
                    (
                        app.state.settings.auth_session_cookie_name
                        + "="
                        + session.access.session_token
                    ).encode(),
                ),
                (b"x-csrf-token", session.access.csrf_token.encode()),
            ],
            "state": {"request_id": str(session.access.request_id)},
            "query_string": b"",
        }
    )
    actor = AuthenticatedActor(
        user_id=session.publisher,
        organization_id=session.organization_id,
        email="publisher@example.test",
        display_name="Synthetic publisher",
        system_role="ADMIN",
    )
    before = session.frozen_values()
    with pytest.raises(ProblemException) as failure:
        await publish_skill_version(
            request=request,
            skill_version_id=session.version.id,
            body=PublishSkillVersionRequest(accepted_warnings=[]),
            actor=actor,
        )
    response = await problem_exception_handler(request, failure.value)
    assert response.status_code == 409
    assert response.media_type == "application/problem+json"
    assert b'"code":"skill_publish_gate_failed"' in response.body
    assert marker.encode() not in response.body
    assert session.frozen_values() == before
