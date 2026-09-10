"""包絡の全参照位置、原候補の凍結と主/子で共用する拒否規則を検証する。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from projectmind.agent.outcome import compile_outcome_schema
from projectmind.agent.result_references import (
    EffectSummaryClaim,
    collect_result_references,
)
from projectmind.agent.result_validation import ResultValidationError, ResultValidator
from tests.agent.test_result_validation import MemoryEvidenceLookup, _outcome


class ProposalLookup:
    """実際に照会された集合と Run を保存する scoped test index。"""

    def __init__(self, run_id: UUID, *, exists: bool = True, incomplete: bool = False) -> None:
        """一つの Run にだけ存在する原 Proposal を固定する。"""

        self.run_id, self.exists, self.incomplete = run_id, exists, incomplete
        self.seen: list[frozenset[str]] = []

    async def existing_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """別 Run や欠落の値を通さず、nested ref の照会を記録する。"""

        self.seen.append(refs)
        return refs if run_id == self.run_id and self.exists else frozenset()

    async def incomplete_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """同 Run の未完了記録だけを返す。"""

        return refs if run_id == self.run_id and self.incomplete else frozenset()


def effect_claim(**overrides: Any) -> dict[str, Any]:
    """トップレベル参照配列が空でも照合すべきモデルの主張を返す。"""

    return {
        "proposal_ref": "cp_original", "status": "APPLIED", "summary": "Change recorded",
        "before_ref": "ev_before", "after_ref": "ev_after", **overrides,
    }


async def validate_outcome(validator: ResultValidator, run_id: UUID, value: Any) -> Any:
    """正式包絡と compiled Schema を通す共通呼出し。"""

    compiled = compile_outcome_schema(None, task_schema_checksum=None)
    return await validator.validate(
        run_id=run_id, schema=compiled.schema, schema_ref=compiled.checksum,
        structured_output=value, result_kind="OUTCOME_ENVELOPE",
    )


def test_collects_every_envelope_reference_without_treating_business_fields_as_effects() -> None:
    """ネストした型付き位置と旧 Evidence convention を集め、任意 effects 文字列は無視する。"""

    value = _outcome(
        evidence_refs=["ev_top"], artifact_refs=["art_top"], change_proposal_refs=["cp_top"],
        findings=[{"evidence_refs": ["ev_finding"]}],
        deliverables=[{"artifact_ref": "art_nested"}], effects=[effect_claim()],
        structured_data={
            "source_ref": "ev_business",
            "effects": [{"proposal_ref": "cp_not_platform", "after_ref": "ev_not_platform"}],
        },
    )
    refs = collect_result_references(value, outcome=True)
    assert refs.evidence == {"ev_top", "ev_finding", "ev_business", "ev_before", "ev_after"}
    assert refs.artifacts == {"art_top", "art_nested"}
    assert refs.proposals == {"cp_top", "cp_original"}
    assert refs.effects == (EffectSummaryClaim("cp_original", "APPLIED", "ev_before", "ev_after"),)


@pytest.mark.parametrize("missing", ["ev_before", "ev_after"])
async def test_effect_only_evidence_must_belong_to_run(missing: str) -> None:
    """effects にしかない before/after でも、存在しない/別 Run の根拠は成功にしない。"""

    owned = frozenset({"ev_before", "ev_after"} - {missing})
    with pytest.raises(ResultValidationError) as captured:
        await validate_outcome(
            ResultValidator(MemoryEvidenceLookup(owned)), uuid4(),
            _outcome(evidence_refs=[], effects=[effect_claim()]),
        )
    assert captured.value.code == "evidence_reference_invalid"
    assert missing not in captured.value.message


@pytest.mark.parametrize(
    "exists,incomplete,code", [
        (False, False, "change_proposal_reference_invalid"),
        (True, True, "change_proposal_incomplete"),
    ],
)
async def test_effect_only_proposal_uses_same_ownership_and_waiting_gate(
    exists: bool, incomplete: bool, code: str,
) -> None:
    """モデルがトップレベル proposal_refs から省いても所有/未完了チェックを迂回できない。"""

    run_id = uuid4()
    lookup = ProposalLookup(run_id, exists=exists, incomplete=incomplete)
    validator = ResultValidator(MemoryEvidenceLookup(frozenset({"ev_before", "ev_after"})), lookup)
    with pytest.raises(ResultValidationError) as captured:
        await validate_outcome(
            validator, run_id, _outcome(evidence_refs=[], effects=[effect_claim()]),
        )
    assert captured.value.code == code
    assert lookup.seen == [frozenset({"cp_original"})]


@pytest.mark.parametrize("lookup_present", [False, True])
async def test_effect_status_requires_platform_record_match(lookup_present: bool) -> None:
    """同 Run の Proposal/Evidence があっても効果照合無し・不一致なら APPLIED を保存しない。"""

    run_id = uuid4()
    lookup = AsyncMock()
    lookup.invalid_refs.return_value = frozenset({"cp_original"})
    validator = ResultValidator(
        MemoryEvidenceLookup(frozenset({"ev_before", "ev_after"})), ProposalLookup(run_id),
        effect_lookup=lookup if lookup_present else None,
    )
    with pytest.raises(ResultValidationError) as captured:
        await validate_outcome(
            validator, run_id, _outcome(evidence_refs=[], effects=[effect_claim()]),
        )
    assert captured.value.code == "effect_summary_invalid"
    assert "cp_original" not in captured.value.message


async def test_matched_effect_includes_nested_refs_and_explicit_saved_check_scope() -> None:
    """照合済み集合と保存時の v2 範囲を返し、遠端の未検証を通過と表示しない。"""

    run_id = uuid4()
    lookup = AsyncMock()
    lookup.invalid_refs.return_value = frozenset()
    validated = await validate_outcome(
        ResultValidator(
            MemoryEvidenceLookup(frozenset({"ev_before", "ev_after"})), ProposalLookup(run_id),
            effect_lookup=lookup,
        ), run_id, _outcome(evidence_refs=[], effects=[effect_claim()]),
    )
    assert validated.evidence_refs == {"ev_before", "ev_after"}
    assert validated.change_proposal_refs == {"cp_original"}
    assert validated.validation["reference_checks"] == {
        "version": "projectmind.result-reference-checks/v2", "evidence": "RUN_OWNERSHIP",
        "proposals": "RUN_OWNERSHIP_AND_STATE", "effects": "PLATFORM_RECORD_MATCH",
        "artifacts": "RUN_OWNERSHIP_AND_CONTENT",
    }
    lookup.invalid_refs.assert_awaited_once_with(
        run_id, (EffectSummaryClaim("cp_original", "APPLIED", "ev_before", "ev_after"),),
    )


@pytest.mark.parametrize("nested", [False, True])
async def test_artifact_id_without_saved_snapshot_is_not_a_valid_deliverable(nested: bool) -> None:
    """モデルが生成した art_ prefix や上書き可能 workspace を保存済み Attachment としない。"""

    value = _outcome(evidence_refs=[])
    if nested:
        value["deliverables"] = [
            {"key": "file", "title": "Report", "kind": "artifact", "artifact_ref": "art_missing"},
        ]
    else:
        value["artifact_refs"] = ["art_missing"]
    with pytest.raises(ResultValidationError) as captured:
        await validate_outcome(ResultValidator(MemoryEvidenceLookup(frozenset())), uuid4(), value)
    assert captured.value.code == "artifact_reference_unavailable"
    assert "art_missing" not in captured.value.message


async def test_candidate_copy_prevents_nested_mutation_during_lookup() -> None:
    """所有確認の await 中に元 SDK JSON が変更されても保存する値は検証前の原候補。"""

    value: dict[str, Any] = _outcome(evidence_refs=[])
    original = value["deliverables"][0]["content"]

    class MutatingLookup(MemoryEvidenceLookup):
        """Lookup await の競争相手として元候補を変更する。"""

        async def existing_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
            """既に検証した nested 内容に未検証の効果と artifact を混入させる。"""

            value["effects"].append(effect_claim())
            value["deliverables"][0]["artifact_ref"] = "art_injected"
            value["deliverables"][0]["content"] = "changed-after-validation"
            return await super().existing_refs(run_id, refs)

    validated = await validate_outcome(
        ResultValidator(MutatingLookup(frozenset())), uuid4(), value,
    )
    assert validated.data["effects"] == []
    assert "artifact_ref" not in validated.data["deliverables"][0]
    assert validated.data["deliverables"][0]["content"] == original
