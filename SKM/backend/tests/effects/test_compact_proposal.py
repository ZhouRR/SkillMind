"""省略可能な管理 field と原 SDK identity の決定的補完を検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.proposal import CHANGE_PROPOSE_REQUEST_SCHEMA, parse_change_proposal_request

ROOT = Path(__file__).resolve().parents[3] / "contracts"
ADMIN_FIELDS = (
    "risk_level",
    "reversible",
    "rollback",
    "verification",
    "idempotency_key",
    "expires_in_seconds",
    "continuation_mode",
    "checkpoint",
)


def compact():
    """正規公開例の業務値だけを使い、接続・秘密を作らない。"""
    value = json.loads((ROOT / "examples/change-propose-request.v1.json").read_text())
    return {key: item for key, item in value.items() if key not in ADMIN_FIELDS}


def test_compact_fields_and_fingerprint_preserve_raw_request():
    """原要求を改変せず、回復では raw fingerprint と同じ SDK identity を照合する。"""
    value = compact()
    original = deepcopy(value)
    Draft202012Validator(CHANGE_PROPOSE_REQUEST_SCHEMA).validate(value)
    a = parse_change_proposal_request(value, request_identity="run:attempt:call-1")
    b = parse_change_proposal_request(value, request_identity="run:attempt:call-1")
    c = parse_change_proposal_request(value, request_identity="run:attempt:call-2")
    assert value == original
    assert a.idempotency_key == b.idempotency_key != c.idempotency_key
    assert a.request_fingerprint == sha256_hex(canonical_json(original))
    assert a.verification == {"method": "READ_BACK", "paths": [i["path"] for i in value["changes"]]}
    assert a.continuation_mode == "RESUME" and a.reversible is False
    assert a.checkpoint["confirmed_facts"] == []


@pytest.mark.parametrize(
    "field",
    [
        "resource_key",
        "capability_version",
        "operation",
        "target",
        "changes",
        "precondition",
        "summary",
        "evidence_refs",
    ],
)
def test_business_requirements_are_not_defaulted(field):
    """業務対象・値・根拠の欠落を管理 field のように補わない。"""
    value = compact()
    value.pop(field)
    with pytest.raises(ValueError):
        parse_change_proposal_request(value, request_identity="run:attempt:call-1")


def test_compact_requires_platform_identity():
    """内容だけで要求 ID を作って二つの操作を誤って一つにしない。"""
    with pytest.raises(ValueError):
        parse_change_proposal_request(compact())


def test_explicit_semantics_are_not_overridden():
    """明示された expiry/復旧 mode/前提/期待を上書きしない。"""
    value = compact()
    value.update(
        idempotency_key="original-request-1",
        continuation_mode="REPLACE",
        expires_in_seconds=500,
        reversible=True,
        rollback={"description": "Review first."},
    )
    result = parse_change_proposal_request(value)
    assert result.idempotency_key == "original-request-1"
    assert result.continuation_mode == "REPLACE" and result.reversible is True
    assert result.rollback == value["rollback"]


def test_inline_receipt_matches_public_response_contract():
    """直接交付も公開 Schema へ照合し、原 before/after 値を保持する。"""
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator
    from skillmind.effects.inline import inline_success
    from tests.runs.test_effect_continuation import receipt

    value = receipt()
    response = inline_success(value)
    schema = json.loads(
        (
            Path(__file__).parents[3] / "contracts/tools/change.propose/v1/response.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(response)
    assert response["effect_result"] == value


def test_inline_prompt_preserves_business_stop_conditions():
    """回执の確定を、Skill の条件を無視した次操作の許可と混同しない。"""
    from skillmind.agent.task_brief import build_agent_task_brief, render_task_brief_prompt
    from tests.agent.test_task_brief import (
        RUN_ID,
        _blueprint,
        _limits,
        _manifest,
        _task_snapshot,
        _tools,
    )

    manifest = _manifest(blueprint=_blueprint())
    compiled = build_agent_task_brief(
        run_id=RUN_ID,
        task_snapshot={**_task_snapshot(manifest), "runtime_policy": "skillmind.runtime/v4"},
        manifest=manifest,
        selected_sources={},
        tools=_tools(),
        limits=_limits(),
        model="fixture-model",
    )
    prompt = render_task_brief_prompt(compiled.brief, input_json={}, output_schema={})
    assert "does not satisfy the Skill's business continuation conditions" in prompt
    assert "stop on known failures or unmet conditions" in prompt
    assert "always stop this native turn when the tool returns paused" in prompt
    assert "stop only when" not in prompt


@pytest.mark.parametrize("status", ["ERROR", "TIMEOUT"])
def test_inline_success_preserves_confirmed_business_failure(status):
    """配送成功は実操作の成功でない。元の失敗状態と回读の意味を保持する。"""
    from skillmind.effects.inline import inline_success
    from tests.runs.test_effect_continuation import receipt

    original = receipt()
    original["capability_version"] = "mcp.call/v1"
    original["after"] = {"read_back": {"status": status, "actual": "hallo\r"}}
    original["after_content_hash"] = "sha256:" + sha256_hex(canonical_json(original["after"]))
    original["verification"] = {"business_verdict": "NOT_EVALUATED"}
    before = deepcopy(original)
    result = inline_success(original)
    assert original == before
    assert result["effect_result"] == before
    assert result["effect_result"]["after"]["read_back"]["status"] == status
    assert result["effect_result"]["verification"]["business_verdict"] == "NOT_EVALUATED"
