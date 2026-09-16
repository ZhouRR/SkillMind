"""JSON 検証の外部非取得と、子 process の object 応答境界を確認する。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from referencing import Registry
from referencing.exceptions import NoSuchResource
from skillmind.agent import json_schema_provider
from skillmind.agent.json_schema_validation import ValidationRejected, validate
from skillmind.agent.tool_gateway import ToolProviderError


def test_default_registry_does_not_retrieve_remote_resources() -> None:
    """取得 callback を省いても Registry の既定は URL を取得しない。"""
    with pytest.raises(NoSuchResource):
        Registry().get_or_retrieve("https://schema.example.invalid/never-fetch")


def test_local_schema_references_continue_to_work() -> None:
    """同じ文書の参照は外部取得を閉じても従来通り検証する。"""
    schema = {"$defs": {"value": {"type": "integer"}}, "$ref": "#/$defs/value"}
    assert validate(json.dumps(schema), "1")["valid"] is True
    assert validate(json.dumps(schema), '"text"')["valid"] is False
    with pytest.raises(ValidationRejected, match="invalid_schema"):
        validate('{"$ref":"#/$defs/missing"}', "1")
    with pytest.raises(ValidationRejected, match="external_reference"):
        validate('{"$ref":"https://schema.example.invalid/never-fetch"}', "1")


@pytest.mark.parametrize("result", [None, [], ["error"], "error", 1, {"valid": 1}])
async def test_nonobject_or_invalid_worker_output_is_a_safe_error(monkeypatch, result) -> None:
    """子の契約外 JSON を Any のまま返さず、同じ公開失敗へ閉じる。"""
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(json.dumps(result).encode(), b"")),
    )
    monkeypatch.setattr(
        json_schema_provider.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    with pytest.raises(ToolProviderError) as caught:
        await json_schema_provider._validate_in_process(b"{}")
    assert caught.value.code == "unavailable"
    assert caught.value.message == "JSON validation result is unavailable"


async def test_valid_worker_result_is_preserved(monkeypatch) -> None:
    """検証した object は値や結果判定を補正せず元のまま返す。"""
    result = {"valid": False, "errors": [{"keyword": "type"}], "truncated": False}
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(json.dumps(result).encode(), b"")),
    )
    monkeypatch.setattr(
        json_schema_provider.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    assert await json_schema_provider._validate_in_process(b"{}") == result
