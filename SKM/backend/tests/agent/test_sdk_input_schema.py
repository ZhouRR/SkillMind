"""Claude へ公開する Tool 入力 schema が受理され、原契約の上位集合であることを検証する。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk import create_sdk_mcp_server, tool
from jsonschema import Draft202012Validator
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

from skillmind.agent.claude_tool_schema import ROOT_KEYWORDS, ClaudeToolInputSchema

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "tools"
SCHEMAS = sorted(CONTRACTS.glob("*/*/request.schema.json"))
HASH = "sha256:" + "a" * 64


def _load(capability: str) -> dict:
    """capability (例: change.propose/v1) の request 契約を読む。"""
    return json.loads((CONTRACTS / capability / "request.schema.json").read_text("utf-8"))


def _schema_id(path: Path) -> str:
    return f"{path.parent.parent.name}/{path.parent.name}"


def _server(schema: dict):
    """公開 schema を実 SDK の in-process MCP server へ登録する。"""

    @tool("probe", "probe", ClaudeToolInputSchema(schema).schema)
    async def handler(_arguments):
        return {"content": [{"type": "text", "text": "HANDLER_REACHED"}]}

    return create_sdk_mcp_server("probe", tools=[handler])["instance"]


async def _published(schema: dict) -> dict:
    """実 SDK の list_tools が返す inputSchema を取得する。"""
    response = await _server(schema).request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )
    return response.root.tools[0].inputSchema


async def _call(schema: dict, arguments: dict) -> str:
    """SDK の呼出前検証を含めて呼び、結果本文を返す。"""
    server = _server(schema)
    await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call", params=CallToolRequestParams(name="probe", arguments=arguments)
        )
    )
    return result.root.content[0].text


def _declared(schema: dict) -> set[str]:
    """最上位と各 alternative が宣言する field 名。"""
    names = set(schema.get("properties", {}))
    for key in ("oneOf", "anyOf"):
        for alternative in schema.get(key, []):
            names.update(alternative.get("properties", {}))
    return names


# 原契約が受理する入力。公開 schema も受理できなければ SDK が正当な呼出を拒否する。
VALID_INPUTS = [
    ("change.propose/v1", "inline_mcp_call", {
        "resource_key": "flaui_mcp", "capability_version": "mcp.call/v1", "operation": "call",
        "target": {"locator": "execute_step", "display": "click"},
        "changes": [{"path": "/call", "action": "SET", "value": {"arguments": {}}}],
        "precondition": {"revision": HASH}, "summary": "click",
        "evidence_refs": ["ev_abc"], "verification": {"method": "READ_BACK", "paths": ["/call"]},
    }),
    ("change.propose/v1", "inline_http_write", {
        "resource_key": "api", "capability_version": "http.write/v1", "operation": "post",
        "target": {"locator": "/items", "display": "items"},
        "changes": [{"path": "/x", "action": "SET", "value": 1}], "summary": "s",
        "precondition": {}, "verification": {"method": "HTTP_RESPONSE", "paths": ["/x"]},
    }),
    ("change.propose/v1", "request_file", {
        "request_file": "workspace/p.json", "expected_hash": HASH, "evidence_refs": ["ev_1"],
    }),
    # purpose/response_mode は alternative ごとに定義が異なる。2000 字は file 形式だけが受理する。
    ("mcp.query/v1", "inline", {"name": "inspect", "arguments": {}, "purpose": "p"}),
    ("mcp.query/v1", "file_long_purpose", {
        "request_file": "workspace/q.json", "expected_hash": HASH, "purpose": "p" * 1500,
    }),
    ("mcp.tools/v1", "empty", {}),
    ("workspace.edit/v1", "copy", {
        "operation": "copy", "path": "workspace/a.txt", "source_path": "input/b.txt",
        "expected_hash": HASH, "purpose": "p",
    }),
    ("workspace.edit/v1", "append", {
        "operation": "append", "path": "workspace/a.txt", "text": "t",
        "expected_hash": HASH, "purpose": "p",
    }),
    ("workspace.edit/v1", "publish", {
        "operation": "publish", "path": "output/a.txt", "expected_hash": HASH, "purpose": "p",
    }),
    ("audit.export/v1", "evidence", {
        "path": "output/a.json", "purpose": "p", "evidence_refs": ["ev_1"],
    }),
]


def test_contract_directory_is_found():
    assert any(path.parent.parent.name == "change.propose" for path in SCHEMAS)


@pytest.mark.parametrize("path", SCHEMAS, ids=_schema_id)
@pytest.mark.asyncio
async def test_published_schema_is_accepted_by_claude(path):
    schema = json.loads(path.read_text(encoding="utf-8"))
    published = await _published(schema)

    # Claude CLI は最上位にこれらを持つ Tool を除外する。
    assert not set(ROOT_KEYWORDS) & set(published)
    assert published["type"] == "object"
    # SDK が schema の keyword を引数名として誤公開していれば、ここで一致しない。
    assert set(published["properties"]) == _declared(schema)
    assert published.get("required") == schema.get("required")
    Draft202012Validator.check_schema(published)


@pytest.mark.parametrize("path", SCHEMAS, ids=_schema_id)
def test_adapter_does_not_mutate_the_registered_schema(path):
    schema = json.loads(path.read_text(encoding="utf-8"))
    before = json.dumps(schema, sort_keys=True)

    ClaudeToolInputSchema(schema)

    assert json.dumps(schema, sort_keys=True) == before


@pytest.mark.parametrize(
    ("capability", "arguments"),
    [pytest.param(cap, args, id=f"{cap}:{name}") for cap, name, args in VALID_INPUTS],
)
@pytest.mark.asyncio
async def test_published_schema_accepts_everything_the_contract_accepts(capability, arguments):
    original = _load(capability)
    assert Draft202012Validator(original).is_valid(arguments), "fixture must satisfy the contract"

    published = ClaudeToolInputSchema(original).schema

    assert Draft202012Validator(published).is_valid(arguments)
    assert await _call(original, arguments) == "HANDLER_REACHED"


def test_properties_that_differ_between_alternatives_accept_either_definition():
    properties = ClaudeToolInputSchema(_load("mcp.query/v1")).schema["properties"]

    assert len(properties["purpose"]["anyOf"]) == 2
    assert properties["name"]["type"] == "string"


def test_root_definition_wins_over_narrower_alternative_definition():
    # workspace.edit の alternative は operation を const に絞るが、入力は最上位定義も満たす。
    original = _load("workspace.edit/v1")

    published = ClaudeToolInputSchema(original).schema

    assert published["properties"]["operation"] == original["properties"]["operation"]


def test_dropped_structure_is_described_for_the_model():
    description = ClaudeToolInputSchema(_load("change.propose/v1")).schema["description"]

    assert "Exactly one request form applies" in description
    assert "request_file, expected_hash" in description
    assert "resource_key, capability_version" in description


def test_anyof_contract_keeps_root_properties_and_describes_the_requirement():
    original = _load("audit.export/v1")
    published = ClaudeToolInputSchema(original).schema

    assert "anyOf" not in published
    assert published["properties"] == original["properties"]
    assert "At least one of these field sets is required" in published["description"]


@pytest.mark.asyncio
async def test_sdk_still_rejects_wrong_property_types():
    original = _load("change.propose/v1")

    assert "Input validation error" in await _call(original, {"capability_version": 123})
