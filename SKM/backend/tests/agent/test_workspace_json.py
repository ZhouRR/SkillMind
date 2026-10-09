"""JSON の部分取得が原 byte・読取境界・型と配列の完全性を保つことを検証する。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from skillmind.agent.contract_store import ContractStore
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.agent.workspace_provider import WorkspaceReadProvider
from skillmind.core.hashing import canonical_json, sha256_hex
from tests.agent.test_workspace_provider import (
    CONTRACTS,
    _context,
    _sealed_input,
    _validate_response,
)


def source_file(tmp_path, value, *, root="workspace"):
    """実 Run の file と信頼済み input 回执を用意し、外部資源へは接続しない。"""
    context = _context(tmp_path, "workspace.read/v1")
    path = f"{root}/data.json"
    raw = canonical_json(value).encode()
    (context.workspace.root / path).write_bytes(raw)
    if root == "input":
        context = _sealed_input(context, {"data.json": raw})
    arguments = {"path": path, "expected_hash": "sha256:" + sha256_hex(raw),
                 "purpose": "Read required fields"}
    return context, arguments


@pytest.mark.parametrize("root", ["workspace", "output", "input"])
async def test_selection_preserves_missing_null_false_and_escaped_keys(tmp_path, root):
    """Pointer のエスケープ、零添字と欠落を照合し、実体全体の hash を返す。"""
    context, arguments = source_file(tmp_path, {
        "a/b": {"~key": None}, "enabled": False, "items": [{"id": "first"}],
        "large": "x" * 100_000,
    }, root=root)
    result = await WorkspaceReadProvider().execute(context, {**arguments, "pointers": [
        "/a~1b/~0key", "/enabled", "/missing", "/items/0/id", "/items/01/id",
    ]})
    assert result.response["selections"] == [
        {"pointer": "/a~1b/~0key", "exists": True, "value": None},
        {"pointer": "/enabled", "exists": True, "value": False},
        {"pointer": "/missing", "exists": False},
        {"pointer": "/items/0/id", "exists": True, "value": "first"},
        {"pointer": "/items/01/id", "exists": False},
    ]
    assert result.response["content_hash"] == arguments["expected_hash"]
    assert len(canonical_json(result.response)) < 1_000
    assert result.evidence[0].content_hash == arguments["expected_hash"]
    _validate_response("tools/workspace.read/v1/response.schema.json", dict(result.response))


async def test_array_paging_covers_every_original_item_once(tmp_path):
    """空配列・頁末を明示し、next_offset で元の順序と全件を再現する。"""
    expected = [{"id": i} for i in range(53)]
    context, arguments = source_file(tmp_path, {"items": expected, "empty": []})
    collected, offset = [], 0
    while True:
        result = await WorkspaceReadProvider().execute(context, {
            **arguments, "pointers": ["/items", "/empty"], "array_offset": offset,
        })
        _validate_response("tools/workspace.read/v1/response.schema.json", dict(result.response))
        items, empty = result.response["selections"]
        assert empty["exists"] and empty["value"] == [] and empty["total_items"] == 0
        collected.extend(items["value"])
        if items["next_offset"] is None:
            assert result.response["truncated"] is False
            break
        assert result.response["truncated"] is True
        offset = items["next_offset"]
    assert collected == expected


@pytest.mark.parametrize("damage", ["hash", "escape", "symlink", "unsealed"])
async def test_json_selection_keeps_original_file_boundaries(tmp_path, damage):
    """部分読取でも、改変 byte・別 Run path・symlink・未封存 input を拒否する。"""
    context, arguments = source_file(tmp_path, {"value": "original"})
    if damage == "hash":
        (context.workspace.root / arguments["path"]).write_bytes(b'{"value":"changed"}')
    elif damage == "escape":
        arguments["path"] = "workspace/../data.json"
    elif damage == "symlink":
        other = tmp_path / "other.json"
        other.write_bytes(b'{"value":"other"}')
        (context.workspace.cwd / "link.json").symlink_to(other)
        arguments["path"] = "workspace/link.json"
    else:
        arguments["path"] = "input/data.json"
        context = replace(context, workspace=replace(context.workspace, input_files=None))
    with pytest.raises(ToolProviderError):
        await WorkspaceReadProvider().execute(context, {**arguments, "pointers": ["/value"]})


@pytest.mark.parametrize("extra", [
    {"pointers": ["/bad~2key"]}, {"pointers": ["/value", "/value"]},
    {"pointers": ["/value"], "offset": 0},
    {"pointers": ["/value"], "array_limit": 0},
    {"pointers": ["/value"], "array_limit": True},
    {"pointers": ["/value"], "array_offset": -1},
    {"pointers": ["/value"], "expected_hash": None},
])
async def test_invalid_json_modes_are_rejected_by_contract_and_provider(tmp_path, extra):
    """混用や曖昧な添字を Schema と Provider の両方で拒否する。"""
    context, arguments = source_file(tmp_path, {"value": "original"})
    arguments.update(extra)
    schema = ContractStore(CONTRACTS).load("tools/workspace.read/v1/request.schema.json")
    assert not Draft202012Validator(schema).is_valid(arguments)
    with pytest.raises(ToolProviderError):
        await WorkspaceReadProvider().execute(context, arguments)


@pytest.mark.parametrize("data", [b'{"value":1,"value":2}', b'{"value":NaN}', b'{"value":1e999}'])
async def test_ambiguous_or_non_finite_json_does_not_expose_a_value(tmp_path, data):
    """通常 JSON にない数と重複 key を成功値として返さない。"""
    context = _context(tmp_path, "workspace.read/v1")
    (context.workspace.cwd / "data.json").write_bytes(data)
    with pytest.raises(ToolProviderError) as error:
        await WorkspaceReadProvider().execute(context, {
            "path": "workspace/data.json", "purpose": "Read value", "pointers": ["/value"],
            "expected_hash": "sha256:" + sha256_hex(data),
        })
    assert error.value.code == "invalid_request"


async def test_large_selected_value_is_not_silently_truncated(tmp_path):
    """応答上限で値の後半を消さず、原 hash を保持した小さい選択へ修正できる。"""
    context, arguments = source_file(tmp_path, {"large": "x" * 100_000, "status": "DONE"})
    with pytest.raises(ToolProviderError) as error:
        await WorkspaceReadProvider().execute(context, {**arguments, "pointers": ["/large"]})
    assert error.value.code == "too_large"
    result = await WorkspaceReadProvider().execute(context, {**arguments, "pointers": ["/status"]})
    assert result.response["selections"][0]["value"] == "DONE"


def test_missing_pointer_is_not_a_fabricated_null():
    """公開応答も不存在へ value:null を補う契約ではない。"""
    schema = ContractStore(CONTRACTS).load("tools/workspace.read/v1/response.schema.json")
    example = json.loads((CONTRACTS / "examples/workspace-read-json-response.v1.json").read_text())
    example["selections"] = [{"pointer": "/missing", "exists": False, "value": None}]
    assert not Draft202012Validator(schema).is_valid(example)


async def test_selection_logging_failure_does_not_change_the_read(tmp_path, monkeypatch):
    """観測先が失敗しても、原値・型・hash の読取を続ける。"""
    context, arguments = source_file(tmp_path, {"value": False})

    def unavailable(*args, **kwargs):
        """logger handler の I/O failure を再現する。"""
        raise OSError("synthetic logging failure")

    monkeypatch.setattr("skillmind.core.timing.log_event", unavailable)
    result = await WorkspaceReadProvider().execute(context, {**arguments, "pointers": ["/value"]})
    assert result.response["selections"][0]["value"] is False
