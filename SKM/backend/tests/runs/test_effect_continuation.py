"""実 finalize→Segment→Brief を検証する。SQL/外部サービスは別の回帰責務とする。"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.task_brief import _checkpoint, render_task_brief_prompt
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import Evidence, RunSegment
from skillmind.effects.continuation import (
    EFFECT_RESULT_SCHEMA,
    MAX_EFFECT_RESULT_BYTES,
    validated_effect_result,
)
from skillmind.effects.domain import EffectEvidenceDraft, EffectProviderResult
from skillmind.effects.proposal import CHANGE_PROPOSE_REQUEST_SCHEMA
from skillmind.runs.interaction import INTERACTION_REQUEST_SCHEMA
from tests.agent.test_task_brief import _brief_schema, _build
from tests.runs.test_effect_unknown_outcomes import harness

ROOT = Path(__file__).resolve().parents[3]


def receipt():
    """全 ID と locator が合成値である公開例を使う。"""
    return json.loads((ROOT / "contracts/examples/agent-task-brief-effect.v1.json").read_text())[
        "checkpoint"
    ]["effect_result"]


@pytest.mark.parametrize("capability", ["database.write/v1", "document.write/v1"])
async def test_finalized_original_readback_reaches_next_segment_prompt(capability):
    """再観測せず原 Evidence と同じ byte/hash を渡し、モデルが元 ID を失わない。"""
    h = harness(capability=capability)
    del h.repository._next_effect_segment
    h.proposal.checkpoint_json = {"summary": "Continue after saving.", "evidence_refs": []}
    h.proposal.continuation_mode = "replace"
    original_checkpoint = deepcopy(h.proposal.checkpoint_json)
    content = (receipt()["after"] if capability == "document.write/v1"
               else {"row": {"id": 1, "status": "RUNNING", "generated_state": "READY"}})
    original_content = deepcopy(content)
    evidence = EffectEvidenceDraft(
        capability.split(".")[0], "fixture://receipt", {}, content, None, {},
    )
    await h.repository.finalize_effect_execution(
        h.claimed,
        result=EffectProviderResult(evidence, evidence, {"replayed": True}, True),
        failure=None, duration_ms=1,
    )
    added = [row for call in h.session.add_all.call_args_list for row in call.args[0]]
    next_segment = next(row for row in added if isinstance(row, RunSegment))
    after = next(row for row in added if isinstance(row, Evidence)
                 and row.evidence_ref == h.execution.after_ref)
    confirmed = next_segment.checkpoint_json["effect_result"]
    assert confirmed["effect_execution_id"] == str(h.execution.id)
    assert confirmed["proposal_ref"] == h.proposal.proposal_ref
    assert confirmed["capability_version"] == capability
    assert confirmed["before_ref"] == h.execution.before_ref
    assert confirmed["before_ref"] != confirmed["after_ref"]
    assert confirmed["after_ref"] == after.evidence_ref
    assert confirmed["after"] == after.metadata_json["snapshot"] == content
    assert confirmed["after_content_hash"] == after.content_hash
    assert confirmed["verification"] == h.execution.verification_json == {"replayed": True}
    brief = _build(checkpoint=next_segment.checkpoint_json).brief
    Draft202012Validator(_brief_schema(), format_checker=FormatChecker()).validate(brief)
    prompt = render_task_brief_prompt(brief, input_json={}, output_schema={})
    assert canonical_json(confirmed) in prompt
    assert "not the current remote state" in prompt
    assert "is not its before_ref, even if the row contents are identical" in prompt
    assert h.proposal.checkpoint_json == original_checkpoint
    content[next(iter(content))]["changed"] = "after-finalization"
    assert confirmed["after"] == original_content


@pytest.mark.parametrize("invalid", ["sensitive", "oversize"])
async def test_invalid_provider_return_cannot_finalize_applied_or_dispatch(invalid):
    """公開できない回读は APPLIED にせず、元 claim を回収可能なまま保持する。"""
    h = harness()
    content = ({"password": "synthetic"} if invalid == "sensitive"
               else {"body": "x" * MAX_EFFECT_RESULT_BYTES})
    evidence = EffectEvidenceDraft("database", "fixture://receipt", {}, content, None, {})
    with pytest.raises(ValueError):
        await h.repository.finalize_effect_execution(
            h.claimed, result=EffectProviderResult(evidence, evidence, {}, False),
            failure=None, duration_ms=1,
        )
    assert h.execution.status == "APPLYING"
    assert h.run.status == "WAITING_FOR_APPROVAL"
    h.session.add_all.assert_not_called()
    h.repository._next_effect_segment.assert_not_called()


@pytest.mark.parametrize("outcome", ["REJECTED", "FAILED", "STALE", "EXPIRED"])
def test_unsuccessful_continuation_does_not_reuse_previous_receipt(outcome):
    """拒否/失敗を原成功回执の再表示で成功と誤認させない。"""
    h = harness()
    del h.repository._next_effect_segment
    h.proposal.checkpoint_json = {"effect_result": receipt()}
    h.proposal.continuation_mode = "replace"
    segment = h.repository._next_effect_segment(
        run=h.run, segment=h.segment, proposal=h.proposal,
        trigger_ref=uuid4(), outcome=outcome, now=datetime.now(UTC),
    )
    assert "effect_result" not in segment.checkpoint_json


def test_rejection_feedback_reaches_next_brief_without_mutating_proposal():
    """却下理由を元提案と結んで伝え、拒否を全工程中断や新規許可へ読み替えない。"""
    h = harness()
    del h.repository._next_effect_segment
    h.proposal.checkpoint_json = {"summary": "Proposed final update.", "confirmed_facts": []}
    h.proposal.continuation_mode = "resume"
    original = deepcopy(h.proposal.checkpoint_json)
    reason = 'Finish the required saved report first.\nKeep the existing record.'
    approval_id = uuid4()
    segment = h.repository._next_effect_segment(
        run=h.run, segment=h.segment, proposal=h.proposal,
        trigger_ref=approval_id, outcome="REJECTED", now=datetime.now(UTC),
        rejection_reason=reason,
    )
    assert segment.trigger_ref == approval_id
    assert "effect_result" not in segment.checkpoint_json
    assert h.proposal.checkpoint_json == original
    brief = _build(checkpoint=segment.checkpoint_json).brief
    Draft202012Validator(_brief_schema(), format_checker=FormatChecker()).validate(brief)
    prompt = render_task_brief_prompt(brief, input_json={}, output_schema={})
    facts = brief["checkpoint"]["confirmed_facts"]
    assert facts[-1] == (
        f"User rejection feedback for ChangeProposal {h.proposal.proposal_ref} "
        f"(not write permission): {canonical_json(reason)}"
    )
    assert canonical_json(brief["checkpoint"]) in prompt


def test_non_rejection_cannot_carry_rejection_feedback():
    """失効や失敗の checkpoint に別の拒否理由を混入させない。"""
    h = harness()
    del h.repository._next_effect_segment
    h.proposal.checkpoint_json = {}
    with pytest.raises(ValueError, match="Only a rejection"):
        h.repository._next_effect_segment(
            run=h.run, segment=h.segment, proposal=h.proposal,
            trigger_ref=uuid4(), outcome="STALE", now=datetime.now(UTC),
            rejection_reason="Feedback for another outcome",
        )


@pytest.mark.parametrize("field,value", [
    ("confirmed_facts", ["Original record was saved."]),
    ("evidence_refs", ["ev_saved"]),
    ("effect_result", None),
])
def test_checkpoint_without_summary_is_still_rendered(field, value):
    """summary のない server checkpoint でも事実や回执を prompt から落とさない。"""
    value = receipt() if field == "effect_result" else value
    brief = _build().brief
    brief["checkpoint"] = _checkpoint({field: value})
    prompt = render_task_brief_prompt(brief, input_json={}, output_schema={})
    assert canonical_json(brief["checkpoint"]) in prompt


@pytest.mark.parametrize("schema", [CHANGE_PROPOSE_REQUEST_SCHEMA, INTERACTION_REQUEST_SCHEMA])
def test_model_checkpoint_cannot_supply_platform_effect_result(schema):
    """モデルからの propose/interaction には server 専用 field を許さない。"""
    checkpoint_schema = schema.get("$defs", {}).get("checkpoint") or schema["properties"][
        "checkpoint"
    ]
    base = {"summary": "Saved", "confirmed_facts": [], "evidence_refs": [],
            "artifact_refs": [], "change_proposal_refs": []}
    validator = Draft202012Validator(checkpoint_schema)
    assert validator.is_valid(base)
    assert not validator.is_valid({**base, "effect_result": receipt()})


def test_effect_result_contract_matches_runtime_and_legacy_checkpoint_omits_field():
    """公開 contract と同一規則を使い、旧 checkpoint の値/checksum を増補しない。"""
    assert _brief_schema()["$defs"]["effectResult"] == EFFECT_RESULT_SCHEMA
    assert "effect_result" not in _checkpoint({})


def test_legacy_receipt_without_before_ref_is_preserved():
    """旧回执の checksum を変えず、未知の前態参照を補造しない。"""
    legacy = receipt()
    legacy.pop("before_ref")
    assert validated_effect_result(legacy) == legacy
    assert _checkpoint({"effect_result": legacy})["effect_result"] == legacy
    brief = _build(checkpoint={"effect_result": legacy}).brief
    Draft202012Validator(_brief_schema(), format_checker=FormatChecker()).validate(brief)
    prompt = render_task_brief_prompt(brief, input_json={}, output_schema={})
    assert "If an older receipt omits before_ref, omit that optional field" in prompt


@pytest.mark.parametrize("invalid", ["hash", "sensitive", "oversize", "status", "shape"])
def test_corrupt_or_unsafe_receipt_is_rejected_before_agent(invalid):
    """切詰めや推測で成功にせず、回执本文の破損と公開不可値を拒否する。"""
    value = receipt()
    if invalid == "hash":
        value["after"]["document"]["path"] = "tampered.md"
    elif invalid == "sensitive":
        value["verification"]["password"] = "synthetic"
    elif invalid == "oversize":
        value["after"] = {"body": "文" * (MAX_EFFECT_RESULT_BYTES // 2)}
        value["after_content_hash"] = "sha256:" + sha256_hex(canonical_json(value["after"]))
    elif invalid == "status":
        value["status"] = "FAILED"
    else:
        value["extra"] = True
    with pytest.raises(ValueError):
        validated_effect_result(value)
