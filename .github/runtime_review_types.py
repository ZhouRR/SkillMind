"""検証済み runtime 差分へ小さな型境界整理を加え、同じ検証対象へ登録する。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

state = Path(os.environ["RUNNER_TEMP"]) / "runtime-review-paths.json"
paths = set(json.loads(state.read_text()))


def patch(path, sha, replacements):
    raw = Path(path).read_bytes()
    assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == sha
    text = raw.decode("utf-8")
    for old, new in replacements:
        assert text.count(old) == 1
        text = text.replace(old, new, 1)
    Path(path).write_text(text, encoding="utf-8")
    paths.add(path)


patch("SKM/backend/src/skillmind/agent/json_schema_validation.py", "99aad1c8d0c6d75406c71aea12d4406740de69ae", [
    ("from referencing.exceptions import NoSuchResource, Unresolvable", "from referencing.exceptions import Unresolvable"),
    ('def _no_resource(uri: str) -> Any:\n    """Schema の ID は識別子に限り、URL/file の取得は常に拒否する。"""\n    raise NoSuchResource(ref=uri)\n\n\n', ""),
    ('    validator = Draft202012Validator(\n        schema, format_checker=checker, registry=Registry(retrieve=_no_resource)\n    )',
     '    # Registry の既定は外部取得を拒否する。別の取得 callback や URL I/O を加えない。\n    validator = Draft202012Validator(schema, format_checker=checker, registry=Registry())'),
])
patch("SKM/backend/src/skillmind/agent/json_schema_provider.py", "5c5f0dbba6a97203928312ae7b64da0e675b31bf", [
    ('        result = json.loads(stdout)\n        if "error" in result:',
     '        result = json.loads(stdout)\n        if not isinstance(result, dict):\n            raise ValueError("Invalid validator response")\n        if "error" in result:'),
])
path = Path("SKM/backend/tests/agent/test_json_validation_boundary.py")
assert not path.exists()
path.write_text('''"""JSON 検証の外部非取得と、子 process の object 応答境界を確認する。"""

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
        returncode=0, communicate=AsyncMock(return_value=(json.dumps(result).encode(), b"")),
    )
    monkeypatch.setattr(json_schema_provider.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(ToolProviderError) as caught:
        await json_schema_provider._validate_in_process(b"{}")
    assert caught.value.code == "unavailable"
    assert caught.value.message == "JSON validation result is unavailable"


async def test_valid_worker_result_is_preserved(monkeypatch) -> None:
    """検証した object は値や結果判定を補正せず元のまま返す。"""
    result = {"valid": False, "errors": [{"keyword": "type"}], "truncated": False}
    process = SimpleNamespace(
        returncode=0, communicate=AsyncMock(return_value=(json.dumps(result).encode(), b"")),
    )
    monkeypatch.setattr(json_schema_provider.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert await json_schema_provider._validate_in_process(b"{}") == result
''', encoding="utf-8")
paths.add(str(path))
state.write_text(json.dumps(sorted(paths)), encoding="utf-8")
