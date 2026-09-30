"""提案入口の説明到達性と、説明した最小要求の実 Effect 契約との整合を検証する。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from skillmind.agent.contract_store import ContractStore
from skillmind.agent.tool_catalog import create_run_tool_registry
from skillmind.effects.catalog import EFFECT_CAPABILITIES
from skillmind.effects.document_write import validate_document_write_proposal
from skillmind.effects.domain import ChangeProposalValidationError
from skillmind.effects.proposal import parse_change_proposal_request
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _context, _registry
from tests.documents.test_document_library_binding import target

CONTRACTS = ContractStore(Path(__file__).resolve().parents[3] / "contracts")


def proposal_tool():
    """外部 Provider を呼ばず、本物の catalog から SDK 公開契約を解決する。"""
    return create_run_tool_registry(
        CONTRACTS, document_source=Mock(),
    ).resolve_unbound("change.propose/v1", execution_profile="SUPERVISED")


@pytest.fixture
def tool_runtime(tmp_path):
    """両 SDK が参照する本物の Gateway 説明表を、外部 I/O なしで組み立てる。"""
    registry = create_run_tool_registry(
        CONTRACTS, document_source=Mock(), mcp_tools_provider=Mock(),
    )
    proposal = registry.resolve_unbound("change.propose/v1", execution_profile="SUPERVISED")
    discovery = registry.resolve(
        "mcp.tools/v1", provider="mcp", integration_id=uuid4(), binding_id=uuid4(),
    )
    context = replace(
        _context(tmp_path, _registry(CsvIssueProvider())), tools=(proposal, discovery),
        permission_snapshot={
            "mode": "auto_read_only", "execution_profile": "SUPERVISED",
            "allowed_capabilities": ["change.propose/v1", "mcp.tools/v1"],
        },
    )
    runtime = registry.build_gateway_runtime(context, audit_writer=MemoryAuditWriter())
    return registry, runtime, proposal, discovery


@pytest.mark.parametrize("capability", sorted(EFFECT_CAPABILITIES))
def test_every_effect_recipe_reaches_the_proposal_tool(capability, tool_runtime):
    """別の読取工具を呼ばなくても、全登録 Effect の正確な要求説明に到達する。"""
    _, runtime, tool, _ = tool_runtime
    published = runtime.tool_descriptions[tool.sdk_name]
    description = CONTRACTS.load(f"tools/{capability}/request.schema.json")["description"]
    assert published.count(f"{capability}: {description}") == 1
    assert "not permission" in published


def test_mcp_discovery_does_not_duplicate_the_proposal_recipe(tool_runtime):
    """MCP 発見は提案入口を案内し、長い回読・取消説明を二重注入しない。"""
    registry, runtime, _, tool = tool_runtime
    published = runtime.tool_descriptions[tool.sdk_name]
    assert "change.propose/v1" in published
    assert CONTRACTS.load("tools/mcp.call/v1/request.schema.json")["description"] not in (
        published
    )
    with pytest.raises(LookupError):
        registry.resolve(
            "mcp.call/v1", provider="mcp", integration_id=uuid4(), binding_id=uuid4(),
        )


def document_request(operation, value):
    """説明通りの最小提案。回読や冪等 key は原 SDK identity から platform が導出する。"""
    return {
        "resource_key": "outputs",
        "capability_version": "document.write/v1",
        "operation": operation,
        "target": {"locator": "results/spec.md", "display": "Specification"},
        "changes": [{"path": "/document", "action": "SET", "value": value}],
        "precondition": {
            "revision": "absent" if operation in {"CREATE", "CREATE_FOLDER"}
            else "sha256:" + "b" * 64,
        },
        "evidence_refs": ["ev_original"],
        "summary": "Save or organize the reviewed specification",
    }


@pytest.mark.parametrize("operation", ["CREATE", "UPDATE"])
@pytest.mark.parametrize("source", ["artifact.append", "document.convert"])
def test_document_save_recipe_uses_original_receipt_metadata(operation, source, tool_runtime):
    """変換・追記のサイズ項目を保存契約へ正確に渡し、既定回読も /document になる。"""
    receipt = {"artifact_refs": ["art_original"]}
    if source == "artifact.append":
        receipt.update(content_hash="sha256:" + "a" * 64, bytes_written=120)
        checksum = receipt["content_hash"]
        size = receipt["bytes_written"]
    else:
        receipt["document"] = {"checksum": "sha256:" + "c" * 64, "size": 90}
        receipt["artifact"] = {"content_hash": "sha256:" + "a" * 64, "size_bytes": 120}
        checksum = receipt["artifact"]["content_hash"]
        size = receipt["artifact"]["size_bytes"]
    value = {
        "artifact_ref": receipt["artifact_refs"][0], "content_hash": checksum,
        "size_bytes": size, "mime_type": "text/markdown",
    }
    request = document_request(operation, value)
    _, runtime, tool, _ = tool_runtime
    published = runtime.tool_descriptions[tool.sdk_name]
    Draft202012Validator(tool.input_schema).validate(request)
    draft = parse_change_proposal_request(request, request_identity="fixture:attempt:call")
    payload = validate_document_write_proposal(draft, binding_scope=target().scope(uuid4()))
    assert {key: payload[key] for key in value} == value
    assert payload["content_hash"] == "sha256:" + "a" * 64
    assert payload["size_bytes"] == 120
    assert draft.verification == {"method": "READ_BACK", "paths": ["/document"]}
    assert f"{source} uses" in published
    for key in value:
        assert key in published


@pytest.mark.parametrize(
    "operation,value",
    [
        ("CREATE_FOLDER", {}), ("TRASH", {}), ("DELETE_FOLDER", {}),
        ("MOVE", {"destination": "results/renamed.md"}),
        ("MOVE_FOLDER", {"destination": "archive/results"}),
        ("RESTORE", {"document_id": "10000000-0000-0000-0000-000000000001"}),
    ],
)
def test_document_management_recipe_preserves_operation_specific_fields(operation, value):
    """空 value・移動先・原ごみ箱 ID の差を保ち、管理操作も同じ提案契約を通る。"""
    request = document_request(operation, value)
    Draft202012Validator(proposal_tool().input_schema).validate(request)
    draft = parse_change_proposal_request(request, request_identity="fixture:attempt:call")
    payload = validate_document_write_proposal(draft, binding_scope=target().scope(uuid4()))
    assert payload["operation"] == operation
    assert payload["expected_revision"] == request["precondition"]["revision"]
    for key, expected in value.items():
        assert payload[key] == expected


@pytest.mark.parametrize("path", ["/artifact_ref", "/artifact", "/content"])
def test_document_recipe_does_not_relax_rejected_aliases(path):
    """説明補足で原契約を広げず、実障害で使われた別 path を引き続き拒否する。"""
    request = document_request("CREATE", {
        "artifact_ref": "art_original", "content_hash": "sha256:" + "a" * 64,
        "size_bytes": 120, "mime_type": "text/markdown",
    })
    request["changes"][0]["path"] = path
    draft = parse_change_proposal_request(request, request_identity="fixture:attempt:call")
    with pytest.raises(ChangeProposalValidationError):
        validate_document_write_proposal(draft, binding_scope=target().scope(uuid4()))
