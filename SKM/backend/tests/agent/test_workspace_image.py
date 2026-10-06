"""実画像の型・hash・path と、監査後の MCP image 交付を検証する。"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from mcp.types import ImageContent
from PIL import Image

from skillmind.agent import workspace_image
from skillmind.agent.codex_mcp import CodexToolBridge
from skillmind.agent.contract_store import ContractStore
from skillmind.agent.tool_catalog import create_run_tool_registry
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.agent.workspace_image import CAPABILITY, WorkspaceImageProvider
from skillmind.core.hashing import sha256_hex
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _registry
from tests.agent.test_tool_gateway import _context as run_context
from tests.agent.test_workspace_provider import _context, _sealed_input


def image_bytes(format: str = "PNG") -> bytes:
    """実 decoder で読める小さい色付き画像だけを生成し、業務情報を含めない。"""
    output = io.BytesIO()
    Image.new("RGB", (4, 3), (32, 80, 160)).save(output, format=format)
    return output.getvalue()


def image_run(tmp_path: Path):
    """本番 registry と実 Run workspace を使い、DB/model だけを回帰 port にする。"""
    local = _context(tmp_path, CAPABILITY)
    registry = create_run_tool_registry(
        ContractStore(Path(__file__).parents[3] / "contracts"),
        document_source=Mock(),
    )
    tool = registry.resolve_unbound(CAPABILITY, execution_profile="GUIDED")
    run = replace(
        run_context(tmp_path, _registry(CsvIssueProvider())),
        run_id=local.run_id,
        workspace=local.workspace,
        tools=(tool,),
        permission_snapshot={"mode": "auto_read_only", "allowed_capabilities": [CAPABILITY]},
    )
    return registry, run


@pytest.mark.parametrize(
    "format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
async def test_real_image_format_and_hash_are_audited_without_base64(tmp_path, format, mime):
    """拡張子に頼らず実画像を確認し、監査結果には原 hash と寸法だけを保存する。"""
    context = _context(tmp_path, CAPABILITY)
    data = image_bytes(format)
    (context.workspace.cwd / "response.bin").write_bytes(data)
    result = await WorkspaceImageProvider().execute(
        context,
        {
            "path": "workspace/response.bin",
            "expected_hash": "sha256:" + sha256_hex(data),
        },
    )
    descriptor = result.response["image"]
    assert descriptor["mime_type"] == mime and (descriptor["width"], descriptor["height"]) == (4, 3)
    assert result.evidence[0].content_hash == descriptor["content_hash"]
    assert base64.b64encode(data).decode() not in json.dumps(result.response)


@pytest.mark.parametrize(
    "data",
    [b'<svg xmlns="http://www.w3.org/2000/svg"/>', b"<html>image</html>", b"\x89PNG\r\n\x1a\n"],
)
async def test_non_images_and_corrupt_images_are_not_delivered(tmp_path, data):
    """名前/MIME 偽装と壊れた PNG をモデルへ転送しない。"""
    context = _context(tmp_path, CAPABILITY)
    (context.workspace.cwd / "image.png").write_bytes(data)
    with pytest.raises(ToolProviderError, match="Image is invalid"):
        await WorkspaceImageProvider().execute(
            context,
            {
                "path": "workspace/image.png",
                "expected_hash": "sha256:" + sha256_hex(data),
            },
        )


@pytest.mark.parametrize(
    "damage", ["hash", "symlink", "outside", "unsealed_input", "bytes", "pixels", "animation"]
)
async def test_image_reading_preserves_scope_hash_and_size_limits(tmp_path, monkeypatch, damage):
    """未知 input、root 越境、変更、link、画像予算と動画を拒否する。"""
    context = _context(tmp_path, CAPABILITY)
    data = image_bytes()
    target = context.workspace.cwd / "image.png"
    target.write_bytes(data)
    path, checksum = "workspace/image.png", "sha256:" + sha256_hex(data)
    if damage == "hash":
        checksum = "sha256:" + "0" * 64
    elif damage == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "outside.png")
    elif damage == "outside":
        path = "workspace/../../outside.png"
    elif damage == "unsealed_input":
        (context.workspace.input_dir / "image.png").write_bytes(data)
        path = "input/image.png"
    elif damage == "bytes":
        monkeypatch.setattr(workspace_image, "MAX_IMAGE_BYTES", len(data) - 1)
    elif damage == "pixels":
        monkeypatch.setattr(workspace_image, "MAX_IMAGE_PIXELS", 3)
    else:
        output = io.BytesIO()
        Image.new("RGB", (4, 3), "red").save(
            output,
            format="PNG",
            save_all=True,
            append_images=[Image.new("RGB", (4, 3), "blue")],
        )
        data = output.getvalue()
        target.write_bytes(data)
        checksum = "sha256:" + sha256_hex(data)
    with pytest.raises(ToolProviderError):
        await WorkspaceImageProvider().execute(context, {"path": path, "expected_hash": checksum})


async def test_frozen_input_image_uses_the_original_seal(tmp_path):
    """封存入力を通常の画像として読めるが、信頼回执のない入力とは区別する。"""
    context = _context(tmp_path, CAPABILITY)
    data = image_bytes()
    context = _sealed_input(context, {"image.png": data})
    (context.workspace.input_dir / "image.png").write_bytes(data)
    result = await WorkspaceImageProvider().execute(
        context,
        {
            "path": "input/image.png",
            "expected_hash": "sha256:" + sha256_hex(data),
        },
    )
    assert result.response["image"]["path"] == "input/image.png"


async def test_gateway_and_codex_bridge_deliver_images_after_audit_and_validate_replay(tmp_path):
    """画像は typed MCP content として渡し、成功再読取でも改変 file を再交付しない。"""
    registry, run = image_run(tmp_path)
    data = image_bytes()
    target = run.workspace.cwd / "image.png"
    target.write_bytes(data)
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(run, audit_writer=writer)
    bridge = CodexToolBridge(run, runtime, on_deferred=AsyncMock())
    bridge.session_id = str(uuid4())
    arguments = {"path": "workspace/image.png", "expected_hash": "sha256:" + sha256_hex(data)}
    first = await bridge.invoke(run.tools[0].sdk_name, arguments, "image-original")
    assert len(writer.completed) == 1
    assert isinstance(first.content[1], ImageContent)
    assert base64.b64decode(first.content[1].data) == data
    assert first.content[1].mimeType == "image/png"
    assert "data" not in json.loads(first.content[0].text)["image"]
    assert await bridge.invoke(run.tools[0].sdk_name, arguments, "image-original") == first
    assert len(writer.completed) == 1
    target.write_bytes(image_bytes("JPEG"))
    rejected = await bridge.invoke(run.tools[0].sdk_name, arguments, "image-original")
    assert rejected.isError and len(rejected.content) == 1


async def test_image_encoding_is_included_in_the_existing_output_limit(tmp_path):
    """metadata だけが小さい画像でも、実 content の出力上限を回避しない。"""
    registry, run = image_run(tmp_path)
    data = image_bytes()
    (run.workspace.cwd / "image.png").write_bytes(data)
    run = replace(run, limits=replace(run.limits, max_output_bytes=500))
    runtime = registry.build_gateway_runtime(run, audit_writer=MemoryAuditWriter())
    args = {"path": "workspace/image.png", "expected_hash": "sha256:" + sha256_hex(data)}
    assert runtime.mcp.on_tool_authorized is not None
    await runtime.mcp.on_tool_authorized(run.tools[0].sdk_name, args, "bounded", str(uuid4()))
    result = await runtime.gateway.invoke_mcp(run.tools[0].sdk_name, args)
    assert result["is_error"] and json.loads(result["content"][0]["text"])["code"] == "too_large"


async def test_claude_sdk_mcp_server_keeps_image_content(tmp_path):
    """Claude SDK の実 MCP handler も画像 block を text に変換せず返す。"""
    from mcp.types import CallToolRequest, CallToolRequestParams

    registry, run = image_run(tmp_path)
    data = image_bytes()
    (run.workspace.cwd / "image.png").write_bytes(data)
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(run, audit_writer=writer)
    arguments = {"path": "workspace/image.png", "expected_hash": "sha256:" + sha256_hex(data)}
    assert runtime.mcp.on_tool_authorized is not None
    await runtime.mcp.on_tool_authorized(
        run.tools[0].sdk_name, arguments, "claude-image", str(uuid4())
    )
    server = runtime.mcp.server["instance"]
    result = await server.request_handlers[CallToolRequest](CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name="workspace_image_v1", arguments=arguments),
    ))
    assert len(writer.completed) == 1
    assert isinstance(result.root.content[1], ImageContent)
    assert base64.b64decode(result.root.content[1].data) == data
