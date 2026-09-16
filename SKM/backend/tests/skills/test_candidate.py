"""単一候補の意味保全、原文投影、修復、発行前検証を一連で確認する。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, ValidationError
from skillmind.skills.candidate import candidate_schema, model_request
from skillmind.skills.capability_blueprint import CapabilityBlueprintError
from skillmind.skills.interpreter import InterpreterFixtureRunner, load_interpreter_system_skill
from skillmind.skills.model_interpreter import ModelCompletion, ModelSkillInterpreter
from skillmind.skills.service import SkillService
from skillmind.skills.source_documents import SourceTraceLocationError
from tests.skills.test_skill_service import (
    CATALOG,
    CONTRACTS,
    GENERIC_SKILL,
    SYSTEM_SKILL,
    _fixture_source,
    _InterpretFactory,
    _InterpretSession,
    load_capability_catalog,
)


def candidate() -> dict[str, Any]:
    """独立した native JSON 候補を返す。"""

    return json.loads((CONTRACTS / "examples/skill-candidate.v1.json").read_text())


def request() -> dict[str, Any]:
    """現行候補 Schema と原 source を固定した request を返す。"""

    value = json.loads((CONTRACTS / "examples/skill-interpreter-request.v1.json").read_text())
    value["interpreter"] = load_interpreter_system_skill(
        SYSTEM_SKILL, generation_schema=candidate_schema(CONTRACTS),
    ).to_dict()
    return value


def fill_optionals(value: Any, node: dict[str, Any], schema: dict[str, Any] | None = None) -> Any:
    """Test fixture の省略値だけを native Schema の null に展開する。"""

    schema = schema or candidate_schema(CONTRACTS)
    if "$ref" in node:
        node = schema["$defs"][node["$ref"].removeprefix("#/$defs/")]
    if "anyOf" in node:
        return None if value is None else fill_optionals(value, node["anyOf"][0], schema)
    if node.get("type") == "object":
        return {key: fill_optionals(value[key], child, schema) if key in value else None
                for key, child in node["properties"].items()}
    if node.get("type") == "array":
        return [fill_optionals(item, node["items"], schema) for item in value]
    return value


def test_native_candidate_compiles_once_and_preserves_source_and_input_values() -> None:
    """業務名、混合型 enum、空リストと false を変えず、固定 runtime 部分だけを派生する。"""

    raw = candidate()
    raw["guidance"]["required_rules"] = [{
        "key": "targets", "text": "test_automation.test_document; test_run_id; output/{id}/rv.md",
    }]
    raw["source_traces"].append({
        "target": "/guidance/required_rules/0", "source_ref": "s0:1", "reason": "Exact targets.",
    })
    schema = candidate_schema(CONTRACTS)
    contract = {"contract_version": "skillmind.task-contract-draft/v1", "type": "object",
                "fields": [{"key": "enabled", "type": "boolean", "required": False,
                            "enum": [False, True]}]}
    raw["tasks"][0]["parameter_contract"] = fill_optionals(
        contract, schema["properties"]["tasks"]["items"]["properties"]["parameter_contract"],
    )
    original = deepcopy(raw)
    req = request()
    result = InterpreterFixtureRunner(CONTRACTS).run(req, raw, bind_identity=True)
    manifest = result["runtime_manifest_draft"]
    assert len(manifest["tasks"]) == len(manifest["capability_blueprint"]["tasks"]) == 1
    assert manifest["tasks"][0]["input_schema"]["properties"]["enabled"]["enum"] == [False, True]
    assert "required" not in manifest["tasks"][0]["input_schema"]
    assert manifest["capability_blueprint"]["guidance"]["required_rules"] == (
        raw["guidance"]["required_rules"]
    )
    assert manifest["source_documents"] == req["source"]["source_documents"]
    assert manifest["permissions"]["external_write_policy"] == "deny"
    assert raw == original


def test_prompt_has_one_numbered_copy_and_does_not_change_frozen_request() -> None:
    """正確な原文と番号を提示し、二重の normalized 本文や storage 情報を送らない。"""

    req = request()
    original = deepcopy(req)
    projected = model_request(req)
    assert "source" not in projected and "normalized_package" not in projected
    for source, document in zip(
        projected["sources"], req["source"]["source_documents"], strict=True,
    ):
        restored = "".join(line.split(": ", 1)[1]
                           for line in source["numbered_text"].splitlines(keepends=True))
        assert restored == document["content"]
    assert all(set(c) == {"capability", "description", "providers"}
               for c in projected["capabilities"])
    assert req == original


@pytest.mark.parametrize("reference", ["s999:1", "s0:999999"])
def test_unknown_source_and_out_of_range_line_are_rejected(reference: str) -> None:
    """原文のない ID と越界行を削除・丸めせず修復へ返す。"""

    raw = candidate()
    raw["source_traces"][0]["source_ref"] = reference
    with pytest.raises((CapabilityBlueprintError, SourceTraceLocationError)):
        InterpreterFixtureRunner(CONTRACTS).run(request(), raw, bind_identity=True)


def test_json_string_cannot_replace_native_contract_array() -> None:
    """以前の wire 用 JSON 文字列を通常配列として受理しない。"""

    raw = candidate()
    raw["tasks"][0]["parameter_contract"]["fields"] = "[]"
    with pytest.raises(ValidationError):
        InterpreterFixtureRunner(CONTRACTS).run(request(), raw, bind_identity=True)


@pytest.mark.parametrize("capability", [
    "unknown.read/v1", "database.write/v1", "document.write/v1",
])
def test_publish_only_tool_failures_are_now_detected_before_preview(capability: str) -> None:
    """未登録・直接 write Tool を発行時まで持ち越さない。"""

    raw = candidate()
    raw["tools"] = [{"capability": capability, "required": True,
                     "source_ref": "s0", "reason": "Synthetic tool declaration."}]
    with pytest.raises(CapabilityBlueprintError) as caught:
        InterpreterFixtureRunner(CONTRACTS).run(request(), raw, bind_identity=True)
    assert caught.value.code == "candidate_publish_invalid"
    assert "tool_capability_unregistered" in caught.value.message or (
        "effect_capability_exposed_to_agent" in caught.value.message
    )


def test_input_contract_needs_explicit_source_evidence() -> None:
    """compiler が task 出典から未宣言の入力根拠を捏造しない。"""

    raw = candidate()
    raw["source_traces"] = raw["source_traces"][:1]
    with pytest.raises(CapabilityBlueprintError) as caught:
        InterpreterFixtureRunner(CONTRACTS).run(request(), raw, bind_identity=True)
    assert caught.value.code == "candidate_contract_source_missing"


class RepairClient:
    """候補の一部だけを壊し、二回目の model 入力を確認する transport。"""

    def __init__(self, *, persistent: bool = False) -> None:
        """候補保持と呼出し回数を、test instance 内に限定する。"""

        self.messages: list[str] = []
        self.persistent = persistent

    async def complete(self, *, user_message: str, **kwargs: Any) -> ModelCompletion:
        """初回候補の業務判断が修復 prompt へ保全されることを検査する。"""

        self.messages.append(user_message)
        raw = candidate()
        if len(self.messages) == 1 or self.persistent:
            raw["source_traces"][0]["source_ref"] = "s0:999999"
        return ModelCompletion(structured_output=raw, text=None, truncated=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_service_repair_retains_original_candidate_and_stops_after_two_calls(
    persistent: bool,
) -> None:
    """実 adapter/runner/service を通し、修復上下文・有限回数・失敗停止を確認する。"""

    client = RepairClient(persistent=persistent)
    schema = candidate_schema(CONTRACTS)
    interpreter = ModelSkillInterpreter(
        completion_client=client, system_skill_root=SYSTEM_SKILL, response_schema=schema,
    )
    organization_id, source_id = uuid4(), uuid4()
    source = _fixture_source(GENERIC_SKILL, organization_id, source_id)
    session = _InterpretSession(source, scalars_results=[None, None])
    service = SkillService(
        _InterpretFactory(session), CONTRACTS, interpreter=interpreter,
        capability_catalog=load_capability_catalog(CATALOG),
        interpreter_identity=load_interpreter_system_skill(SYSTEM_SKILL, generation_schema=schema),
    )
    stored = await service.interpret(
        organization_id=organization_id, skill_source_id=source_id, model="synthetic-model",
    )
    assert len(client.messages) == 2
    first = json.JSONDecoder().raw_decode(client.messages[0])[0]
    second = json.JSONDecoder().raw_decode(client.messages[1])[0]
    assert "previous_candidate" not in first
    previous = second.pop("previous_candidate")
    assert previous["source_traces"][0]["source_ref"] == "s0:999999"
    assert first == second
    assert stored.status == ("FAILED" if persistent else "PREVIEW_READY")
    assert len(stored.validation_attempts) == (2 if persistent else 1)


def test_native_schema_has_no_double_encoded_fields_or_presence_wrappers() -> None:
    """定義参照を保持した単一 Schema に文字列退避や二重出力がない。"""

    schema = candidate_schema(CONTRACTS)
    text = json.dumps(schema)
    assert "JSON-encoded" not in text and '"present"' not in text
    assert "runtime_manifest_draft" not in schema["properties"]
    assert "identity" not in schema["properties"]
    assert "report" not in schema["properties"]
    assert len(text) < 25000
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("replacement", [{}, {"response_version": "legacy"}])
def test_native_mode_cannot_fall_back_to_legacy_response(replacement: dict[str, Any]) -> None:
    """model が形式識別子を省略しても native candidate 検証を迂回できない。"""

    with pytest.raises(ValidationError):
        InterpreterFixtureRunner(CONTRACTS).run(
            request(), replacement, bind_identity=True, require_native_candidate=True,
        )


def test_nested_native_contract_preserves_typed_values() -> None:
    """再帰参照経由の object/array を JSON 文字列へ逃がさず task schema に編訳する。"""

    raw = candidate()
    schema = candidate_schema(CONTRACTS)
    contract = {
        "contract_version": "skillmind.task-contract-draft/v1", "type": "object",
        "fields": [{"key": "groups", "type": "array", "required": True,
                    "items": {"type": "object", "fields": [
                        {"key": "mode", "type": "string", "required": True,
                         "enum": ["Quick", "精密"]},
                    ]}}],
    }
    raw["tasks"][0]["parameter_contract"] = fill_optionals(
        contract, schema["properties"]["tasks"]["items"]["properties"]["parameter_contract"],
    )
    result = InterpreterFixtureRunner(CONTRACTS).run(request(), raw, bind_identity=True)
    compiled = result["runtime_manifest_draft"]["tasks"][0]["input_schema"]
    Draft202012Validator(compiled).validate({"groups": [{"mode": "精密"}]})
    with pytest.raises(ValidationError):
        Draft202012Validator(compiled).validate({"groups": [{"mode": "quick"}]})
