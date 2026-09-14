"""JSON 検証の実処理、外部参照拒否、Run file 境界と子プロセス回収を検証する。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from skillmind.agent.json_schema_provider import JsonSchemaValidateProvider, _validate_in_process
from skillmind.agent.json_schema_validation import ValidationRejected, validate
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.core.hashing import sha256_hex
from tests.agent.test_workspace_provider import _context, _sealed_input, _validate_response

CAPABILITY = "json.schema.validate/v1"
SCHEMA = {
    "type": "object",
    "properties": {"id": {"$ref": "#/$defs/id"}},
    "required": ["id"],
    "$defs": {"id": {"type": "string", "format": "uuid"}},
}


@pytest.mark.asyncio
async def test_validates_sealed_schema_and_output_with_format_errors(tmp_path: Path) -> None:
    """封印 Schema と output の hash、format の不適合位置を実 process で確認する。"""
    context = _context(tmp_path, CAPABILITY)
    schema = json.dumps(SCHEMA).encode()
    (context.workspace.input_dir / "schema.json").write_bytes(schema)
    context = _sealed_input(context, {"schema.json": schema})
    arguments = {"schema_path": "input/schema.json", "instance_path": "output/candidate.json"}
    for value, valid in [("not-an-id", False), ("00000000-0000-4000-8000-000000000001", True)]:
        data = json.dumps({"id": value}).encode()
        (context.workspace.output_dir / "candidate.json").write_bytes(data)
        result = await JsonSchemaValidateProvider().execute(context, arguments)
        _validate_response(f"tools/{CAPABILITY}/response.schema.json", dict(result.response))
        assert result.response["valid"] is valid
        assert result.response["instance_hash"] == f"sha256:{sha256_hex(data)}"
        assert result.response["schema_hash"] == f"sha256:{sha256_hex(schema)}"
        assert result.evidence[0].metadata["valid"] is valid
        if not valid:
            assert result.response["errors"][0]["instance_path"] == "/id"
            assert value not in str(result.response)
    (context.workspace.input_dir / "schema.json").write_text("{}")
    with pytest.raises(ToolProviderError):
        await JsonSchemaValidateProvider().execute(context, arguments)


@pytest.mark.parametrize(
    "ref",
    [
        "https://example.invalid/schema",
        "file:///etc/passwd",
        "other.json#/$defs/x",
        "//example.invalid/schema",
    ],
)
def test_denies_external_references_in_unused_branches(ref: str) -> None:
    """外部参照は未選択分岐でも解決前に拒否する。"""
    with pytest.raises(ValidationRejected, match="external_reference"):
        validate(json.dumps({"$defs": {"unused": {"$ref": ref}}}), "null")


@pytest.mark.parametrize(
    ("schema", "instance", "code"),
    [
        ('{"type":"unrecognized"}', "{}", "invalid_schema"),
        ('{"$ref":"#/$defs/missing"}', "{}", "invalid_schema"),
        ('{"format":"custom-unknown"}', '"x"', "unsupported_format"),
        ('{"$schema":"http://json-schema.org/draft-07/schema#"}', "{}", "unsupported_schema"),
        ("{}", '{"x":1,"x":2}', "invalid_json"),
        ("{}", "NaN", "invalid_json"),
        ("{}", "[" * 70 + "0" + "]" * 70, "too_large"),
    ],
)
def test_invalid_or_unsupported_input_is_not_success(schema: str, instance: str, code: str) -> None:
    """校験不能・未対応を validation success と混同しない。"""
    with pytest.raises(ValidationRejected, match=code):
        validate(schema, instance)


def test_bounds_errors_and_ignores_annotation_references() -> None:
    """例データの $ref は取得せず、過大な不適合結果を有限件数で返す。"""
    result = validate(
        json.dumps(
            {"items": {"type": "integer"}, "examples": [{"$ref": "https://example.invalid/"}]}
        ),
        json.dumps(["wrong"] * 100),
    )
    assert result["valid"] is False and result["truncated"] is True
    assert len(result["errors"]) == 50
    assert validate("true", "null")["valid"] is True
    assert validate("false", "null")["valid"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/etc/passwd", "workspace/../secret.json", "temp/x.json", "output/link.json"]
)
async def test_rejects_paths_outside_current_run(tmp_path: Path, path: str) -> None:
    """Traversal、非公開 root、symlink は既存の安全 I/O で拒否する。"""
    context = _context(tmp_path, CAPABILITY)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (context.workspace.output_dir / "link.json").symlink_to(outside)
    with pytest.raises(ToolProviderError):
        await JsonSchemaValidateProvider().execute(
            context, {"schema_path": path, "instance_path": "output/unused.json"}
        )


@pytest.mark.asyncio
async def test_bounds_regex_work_in_real_child_process() -> None:
    """病的な正規表現でも Worker 本体を固めず、子停止後に limit を返す。"""
    payload = json.dumps(
        {"schema": json.dumps({"pattern": "^(a+)+$"}), "instance": json.dumps("a" * 100 + "!")}
    ).encode()
    with pytest.raises(ToolProviderError) as captured:
        await asyncio.wait_for(_validate_in_process(payload), timeout=12)
    assert captured.value.code == "validation_limit"


@pytest.mark.asyncio
async def test_cancellation_reaps_child_and_does_not_forward_app_environment(monkeypatch) -> None:
    """取消は子の終了を待ち、固定 validator にアプリ設定を渡さない。"""
    from skillmind.agent import json_schema_provider

    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        """実 process を観測し、資格情報に見立てた設定が継承されないことを検証する。"""
        assert "DATABASE_URL" not in kwargs["env"]
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setenv("DATABASE_URL", "must-not-propagate")
    monkeypatch.setattr(json_schema_provider.asyncio, "create_subprocess_exec", spawn)
    payload = json.dumps(
        {"schema": json.dumps({"pattern": "^(a+)+$"}), "instance": json.dumps("a" * 100 + "!")}
    ).encode()
    task = asyncio.create_task(_validate_in_process(payload))
    await started.wait()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(processes) == 1 and processes[0].returncode is not None
