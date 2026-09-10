"""実候補からの DRAFT 凍結で、identity 補修や保存 source の取り違えを防ぐ。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Select

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    RuntimeManifest,
    Skill,
    SkillInterpretation,
    SkillSource,
    SkillVersion,
)
from skillmind.skills.domain import (
    SkillInterpretationNotFoundError,
    SkillInterpretationNotReadyError,
    SkillVersionStatus,
    StoredSkillVersion,
)
from tests.skills.test_skill_publication_validation import PublicationResult, PublicationSession


class DraftSession(PublicationSession):
    """本番 create SELECT と FK flush 境界を持つ、未発行候補専用の session。"""

    def __init__(self, *, existing_skill: bool = False) -> None:
        """Interpreter の仮 ID を残す候補を source/interpretation と共に保存する。"""
        super().__init__()
        self.rows = (self.source, self.interpretation)
        if existing_skill:
            self.rows += (self.skill,)
        self.persisted = {row.id for row in self.rows}
        self.added: list[Skill | SkillVersion | RuntimeManifest] = []
        self.pending: list[Skill | SkillVersion | RuntimeManifest] = []
        candidate = self.interpretation.manifest_draft_json
        self.placeholder_id = str(uuid4())
        candidate["identity"]["interpretation_id"] = self.placeholder_id
        candidate["capability_blueprint"]["identity"]["interpretation_id"] = self.placeholder_id
        self.flushes: list[tuple[type, ...]] = []

    async def scalars(self, statement: Select[tuple[Any, ...]]) -> PublicationResult:
        """元の WHERE 条件ごとに候補/Skill/版番号を解決し、別 aggregate を混ぜない。"""
        sql = str(statement)
        self.queries.append(sql)
        parameters = statement.compile().params
        if sql.startswith("SELECT skill_versions.version"):
            assert "skill_versions.skill_id =" in sql
            assert parameters == {"skill_id_1": self.skill.id}
            versions = [row.version for row in self.rows if isinstance(row, SkillVersion)]
            assert len(versions) <= 1
            return PublicationResult(versions[0] if versions else None)
        if "FROM skill_versions" in sql:
            assert "skill_versions.interpretation_id =" in sql
            assert parameters == {"interpretation_id_1": self.interpretation.id}
            return PublicationResult(
                next(
                    (
                        row
                        for row in self.rows
                        if isinstance(row, SkillVersion)
                        and row.interpretation_id == self.interpretation.id
                    ),
                    None,
                )
            )
        if "FROM skills" in sql:
            assert "skills.organization_id =" in sql and "skills.key =" in sql
            candidate = self.interpretation.manifest_draft_json
            assert parameters == {
                "organization_id_1": self.organization_id,
                "key_1": candidate["identity"]["skill_key"],
            }
            return PublicationResult(
                next(
                    (
                        row
                        for row in self.rows
                        if isinstance(row, Skill)
                        and row.organization_id == parameters["organization_id_1"]
                        and row.key == parameters["key_1"]
                    ),
                    None,
                )
            )
        return await super().scalars(statement)

    def add(self, model: Skill | SkillVersion | RuntimeManifest) -> None:
        """relationship のない FK の親を同一 flush の並び順で解決したことにしない。"""
        if isinstance(model, SkillVersion):
            assert model.skill_id in self.persisted, "Skill must be flushed before SkillVersion"
            self.version = model
        elif isinstance(model, RuntimeManifest):
            assert model.skill_version_id in self.persisted, "Version must precede Manifest"
            self.manifest = model
        else:
            self.skill = model
        self.added.append(model)
        self.pending.append(model)
        self.rows += (model,)

    async def flush(self) -> None:
        """親行が保存済みになった順序を観測し、実 transaction の証明とは区別する。"""
        self.flushes.append(tuple(type(row) for row in self.pending))
        self.persisted.update(row.id for row in self.pending)
        self.pending.clear()
        await super().flush()

    async def draft(self) -> StoredSkillVersion:
        """実 Service に元 Interpretation の候補だけを凍結させる。"""
        return await self.service().create_version_draft(
            access=self.access,
            interpretation_id=self.interpretation.id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_skill", [False, True])
async def test_draft_only_rebinds_matching_interpretation_ids_and_hashes_original_manifest(
    existing_skill: bool,
) -> None:
    """既定値で候補を補完せず、同じ仮 ID 二箇所だけを実 ID へ凍結する。"""
    session = DraftSession(existing_skill=existing_skill)
    candidate = deepcopy(session.interpretation.manifest_draft_json)
    # 原 Blueprint の任意項目を省略し、normalizer による空集合追加を検出する。
    for name in ("execution_preferences", "assumptions", "questions"):
        session.interpretation.manifest_draft_json["capability_blueprint"].pop(name)
        candidate["capability_blueprint"].pop(name)
    expected = deepcopy(candidate)
    expected["identity"]["interpretation_id"] = str(session.interpretation.id)
    expected["capability_blueprint"]["identity"]["interpretation_id"] = str(
        session.interpretation.id
    )
    result = await session.draft()
    assert result.status is SkillVersionStatus.DRAFT
    assert result.gate_passed is True
    assert result.manifest == expected
    assert result.manifest_checksum == "sha256:" + sha256_hex(canonical_json(expected))
    assert session.interpretation.manifest_draft_json == candidate
    assert session.flushes == (
        [(Skill,), (SkillVersion,), (RuntimeManifest,)]
        if not existing_skill
        else [(SkillVersion,), (RuntimeManifest,)]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "source-text",
        "source-index",
        "source-hash",
        "interpreter-version",
        "source-path",
        "other-task",
        "bad-trace",
        "different-blueprint-id",
        "blueprint-already-real-id",
        "same-noncanonical-uuid",
        "same-nil-uuid",
        "different-blueprint-skill",
        "missing-original-blueprint-id",
        "null-guidance",
    ],
)
async def test_invalid_candidate_is_saved_as_failed_gate_without_repair(case: str) -> None:
    """審査用 DRAFT は原破損を保持し、正しい新 hash を成功の証明と混同しない。"""
    session = DraftSession(existing_skill=True)
    manifest = session.interpretation.manifest_draft_json
    blueprint = manifest["capability_blueprint"]
    if case == "source-text":
        session.source.source_snapshot_json[0]["content"] += "Changed source"
    elif case == "source-index":
        session.source.source_file_index_json[0]["size"] += 1
    elif case == "source-hash":
        manifest["identity"]["source_hash"] = "sha256:" + "f" * 64
    elif case == "interpreter-version":
        manifest["identity"]["interpreter_version"] = "foreign-interpreter/9"
    elif case == "source-path":
        session.source.source_snapshot_json[0]["path"] = "../private.md"
    elif case == "other-task":
        manifest["tasks"][0]["capability"] = "wrong.capability"
    elif case == "bad-trace":
        blueprint["source_traces"][0]["target"] = "/tasks/1/missing"
    elif case == "different-blueprint-id":
        blueprint["identity"]["interpretation_id"] = str(uuid4())
    elif case == "blueprint-already-real-id":
        blueprint["identity"]["interpretation_id"] = str(session.interpretation.id)
    elif case in {"same-noncanonical-uuid", "same-nil-uuid"}:
        candidate_id = (
            uuid4().hex
            if case == "same-noncanonical-uuid"
            else "00000000-0000-0000-0000-000000000000"
        )
        manifest["identity"]["interpretation_id"] = candidate_id
        blueprint["identity"]["interpretation_id"] = candidate_id
    elif case == "different-blueprint-skill":
        blueprint["identity"]["skill_key"] = "different-skill"
    elif case == "missing-original-blueprint-id":
        del blueprint["identity"]["interpretation_id"]
    else:
        blueprint["guidance"] = None
    original = deepcopy(manifest)
    result = await session.draft()
    assert result.status is SkillVersionStatus.DRAFT
    assert result.gate_passed is False
    assert any(finding.severity == "error" for finding in result.gate_findings)
    assert session.interpretation.manifest_draft_json == original
    expected = deepcopy(original)
    if case not in {
        "different-blueprint-id",
        "blueprint-already-real-id",
        "missing-original-blueprint-id",
        "same-noncanonical-uuid",
        "same-nil-uuid",
    }:
        expected["identity"]["interpretation_id"] = str(session.interpretation.id)
        expected["capability_blueprint"]["identity"]["interpretation_id"] = str(
            session.interpretation.id
        )
    assert result.manifest == expected
    assert result.manifest_checksum == "sha256:" + sha256_hex(canonical_json(expected))


@pytest.mark.asyncio
async def test_parser_without_blueprint_can_save_unpublishable_draft() -> None:
    """parse の未宣言を Blueprint の逆生成や発行可能への昇格に使わない。"""
    session = DraftSession(existing_skill=True)
    session.interpretation.origin = "deterministic_parser"
    session.interpretation.manifest_draft_json["capability_blueprint"] = None
    result = await session.draft()
    assert result.status is SkillVersionStatus.DRAFT
    assert result.gate_passed is False
    assert "capability_blueprint_missing" in {finding.code for finding in result.gate_findings}
    assert result.manifest["capability_blueprint"] is None


@pytest.mark.asyncio
async def test_parser_cannot_claim_a_model_blueprint() -> None:
    """non-model の候補に Blueprint が存在しても発行経路へ取り込まない。"""
    session = DraftSession(existing_skill=True)
    session.interpretation.origin = "deterministic_parser"
    before = session.frozen_values()
    with pytest.raises(SkillInterpretationNotReadyError):
        await session.draft()
    assert session.frozen_values() == before
    assert session.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign-source", "missing-source", "missing-interpretation"])
async def test_invisible_candidate_does_not_create_draft(case: str) -> None:
    """元 source の組織/不存在を候補に書かれた identity で補完しない。"""
    session = DraftSession(existing_skill=True)
    if case == "foreign-source":
        session.source.organization_id = uuid4()
    else:
        session.missing.add(SkillSource if case == "missing-source" else SkillInterpretation)
    before = session.frozen_values()
    with pytest.raises(SkillInterpretationNotFoundError):
        await session.draft()
    assert session.frozen_values() == before
    assert session.added == []


@pytest.mark.asyncio
async def test_repeated_draft_returns_first_frozen_manifest_without_new_rows() -> None:
    """同じ Interpretation は新しく正規化せず、最初の DRAFT/hash を再読する。"""
    session = DraftSession(existing_skill=True)
    first = await session.draft()
    frozen = session.frozen_values()
    count = len(session.added)
    second = await session.draft()
    assert second == first
    assert session.frozen_values() == frozen
    assert len(session.added) == count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,error",
    [
        ("foreign-skill", SkillInterpretationNotFoundError),
        ("version-source", SkillInterpretationNotReadyError),
        ("manifest-interpretation", SkillInterpretationNotReadyError),
        ("manifest-version", SkillInterpretationNotReadyError),
    ],
)
async def test_existing_draft_replay_rechecks_aggregate_ownership_without_changing_receipt(
    case: str,
    error: type[Exception],
) -> None:
    """同じ解釈の再送でも別組織/別 source の保存版を原 DRAFT として返さない。"""
    session = DraftSession(existing_skill=True)
    first = await session.draft()
    receipt = deepcopy(first)
    if case == "foreign-skill":
        session.skill.organization_id = uuid4()
    elif case == "version-source":
        session.version.skill_source_id = uuid4()
    elif case == "manifest-interpretation":
        session.manifest.interpretation_id = uuid4()
    else:
        session.manifest.skill_version_id = uuid4()
    frozen = session.frozen_values()
    count = len(session.added)
    with pytest.raises(error):
        await session.draft()
    assert session.frozen_values() == frozen
    assert len(session.added) == count
    assert first == receipt
