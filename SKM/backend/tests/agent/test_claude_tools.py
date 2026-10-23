"""Claude SDK が完全な組合せ Schema を簡略 parameter 宣言と誤認しない。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp.types import ListToolsRequest

from skillmind.agent.claude_tool_schema import ROOT_KEYWORDS, ClaudeToolInputSchema
from skillmind.agent.contract_store import ContractStore
from skillmind.agent.tool_catalog import _change_propose_tool_definition
from skillmind.agent.tool_gateway import ToolRegistry
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _context, _registry


async def test_sdk_lists_original_proposal_alternatives_without_metadata_parameters(tmp_path):
    """実 SDK の tools/list で inline/file の双方を受理し、原契約は変更しない。"""
    contracts = Path(__file__).resolve().parents[3] / "contracts"
    registry = ToolRegistry((_change_propose_tool_definition(ContractStore(contracts)),))
    registered = registry.resolve_unbound("change.propose/v1", execution_profile="GUIDED")
    context = replace(_context(tmp_path, _registry(CsvIssueProvider())), tools=(registered,),
        permission_snapshot={"allowed_capabilities": ["change.propose/v1"]})
    original = dict(registered.input_schema)
    assert "properties" not in original
    runtime = registry.build_gateway_runtime(context, audit_writer=MemoryAuditWriter())
    server = runtime.mcp.server_factory()["instance"]
    response = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )
    advertised = response.root.tools[0].inputSchema
    assert not set(ROOT_KEYWORDS) & advertised.keys()
    assert {"resource_key", "request_file", "expected_hash"} <= advertised["properties"].keys()
    assert advertised.get("required", []) == []
    validator = Draft202012Validator(advertised)
    request = json.loads((contracts / "examples/change-propose-request.v1.json").read_text())
    validator.validate(request)
    validator.validate({
        "request_file": "workspace/proposal.json", "expected_hash": "sha256:" + "a" * 64,
    })
    assert dict(registered.input_schema) == original


@pytest.mark.parametrize("capability,example", [
    ("audit.export", "audit-export"),
    ("document.read", "document-read"),
    ("document.read", "document-read-file"),
    ("document.convert", "document-convert"),
    ("document.files", "document-files"),
    ("database.query", "database-query"),
    ("http.read", "http-read"),
    ("mcp.query", "mcp-query-file"),
    ("workspace.read", "workspace-read-json"),
    ("workspace.edit", "workspace-edit"),
])
def test_sdk_schema_accepts_everything_the_original_contract_accepts(capability, example):
    """公開例と欠落・型誤りの入力で、公開 schema が原契約の受理範囲の上位集合になる。"""
    contracts = Path(__file__).resolve().parents[3] / "contracts"
    original = json.loads((contracts / f"tools/{capability}/v1/request.schema.json").read_text())
    before = deepcopy(original)
    advertised = ClaudeToolInputSchema(original).schema
    validators = [Draft202012Validator(schema) for schema in (original, advertised)]
    request = json.loads((contracts / f"examples/{example}-request.v1.json").read_text())
    assert all(validator.is_valid(request) for validator in validators)
    variants = [{}, {**request, "unexpected": True}]
    for key in request:
        variants.extend([{k: v for k, v in request.items() if k != key}, {**request, key: None}])
    for candidate in variants:
        if validators[0].is_valid(candidate):
            assert validators[1].is_valid(candidate)
    assert original == before
