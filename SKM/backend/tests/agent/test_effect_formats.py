"""Run が許可した外部書込能力の提案形式だけが task brief へ届くことを検証する。"""

from __future__ import annotations

from pathlib import Path

from skillmind.agent.contract_store import ContractStore
from skillmind.agent.effect_formats import effect_formats
from skillmind.agent.task_brief import _effect_instruction

CONTRACTS = ContractStore(Path(__file__).resolve().parents[3] / "contracts")
MCP_OPERATION = {
    "resource_key": "flaui_mcp", "capability_version": "mcp.call/v1", "operation": "call",
    "minimum_risk": "HIGH",
}


def _description(capability: str) -> str:
    return CONTRACTS.load(f"tools/{capability}/request.schema.json")["description"]


def test_only_authorized_capabilities_are_returned_with_their_contract_text():
    policy = {"operations": [
        MCP_OPERATION,
        {**MCP_OPERATION, "resource_key": "other"},
        {"resource_key": "docs", "capability_version": "document.write/v1", "operation": "CREATE"},
    ]}

    formats = effect_formats(CONTRACTS, policy)

    assert formats == {
        "document.write/v1": _description("document.write/v1"),
        "mcp.call/v1": _description("mcp.call/v1"),
    }


def test_unknown_or_malformed_operations_are_ignored():
    policy = {"operations": [
        {"capability_version": "read.only/v1"}, {"capability_version": 7}, "text", {},
    ]}

    assert effect_formats(CONTRACTS, policy) == {}


def test_policy_without_operations_has_no_formats():
    assert effect_formats(CONTRACTS, {"declared_intents": []}) == {}


def test_instruction_lists_the_authorized_format_and_the_universal_requirement():
    policy = {"operations": [MCP_OPERATION]}
    formats = effect_formats(CONTRACTS, policy)

    text = _effect_instruction(policy, formats)

    assert f"Proposal format for mcp.call/v1: {_description('mcp.call/v1')}" in text
    assert "document.write/v1" not in text
    assert "Except http.write/v1" in text
    assert "precondition.revision and evidence_refs" in text
    assert "risk_level, when given, must be at least the minimum_risk" in text


def test_instruction_without_formats_keeps_the_previous_shape():
    text = _effect_instruction({"operations": [MCP_OPERATION]})

    assert "Proposal format for" not in text
    assert text.endswith('"minimum_risk":"HIGH","operation":"call","resource_key":"flaui_mcp"}]')
