"""原 byte 編集の限定置換・競合拒否・公開 Artifact の整合を確認する。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from skillmind.agent.materialization_storage import UnsafeWorkspaceFileError, write_workspace_file
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.agent.workspace_edit import WorkspaceEditProvider
from skillmind.core.hashing import sha256_hex
from tests.agent.test_workspace_provider import _context


def checksum(data):
    """合成 byte の版を独立計算する。"""
    return "sha256:" + sha256_hex(data)


async def test_copy_append_edit_and_publish_preserve_original(tmp_path):
    context = _context(tmp_path, "workspace.edit/v1")
    original = "# 原文\n結論:未確認\n".encode()
    (context.workspace.cwd / "source.md").write_bytes(original)
    provider = WorkspaceEditProvider()
    result = await provider.execute(
        context,
        {
            "operation": "copy",
            "source_path": "workspace/source.md",
            "path": "output/報告.md",
            "expected_hash": checksum(original),
            "purpose": "Copy original bytes",
        },
    )
    result = await provider.execute(
        context,
        {
            "operation": "edit",
            "path": "output/報告.md",
            "expected_hash": result.response["content_hash"],
            "edits": [{"old": "結論:未確認", "new": "結論:確認済み"}],
            "purpose": "Record result",
        },
    )
    result = await provider.execute(
        context,
        {
            "operation": "append",
            "path": "output/報告.md",
            "expected_hash": result.response["content_hash"],
            "text": "範囲:合成例のみ\n",
            "purpose": "Append scope",
        },
    )
    published = await provider.execute(
        context,
        {
            "operation": "publish",
            "path": "output/報告.md",
            "expected_hash": result.response["content_hash"],
            "purpose": "Publish existing file",
        },
    )
    assert (
        published.evidence[0].artifact.content
        == "# 原文\n結論:確認済み\n範囲:合成例のみ\n".encode()
    )
    assert (context.workspace.cwd / "source.md").read_bytes() == original
    assert "content" not in published.response


async def test_stale_hash_duplicate_match_and_existing_copy_target_are_rejected(tmp_path):
    context = _context(tmp_path, "workspace.edit/v1")
    (context.workspace.cwd / "source.md").write_bytes(b"same same")
    (context.workspace.output_dir / "saved.md").write_bytes(b"keep")
    base = {
        "path": "workspace/source.md",
        "expected_hash": checksum(b"same same"),
        "purpose": "Edit",
    }
    provider = WorkspaceEditProvider()
    for request in (
        {**base, "operation": "append", "expected_hash": checksum(b"old"), "text": "x"},
        {**base, "operation": "edit", "edits": [{"old": "same", "new": "other"}]},
        {**base, "operation": "copy", "source_path": base["path"], "path": "output/saved.md"},
    ):
        with pytest.raises(ToolProviderError):
            await provider.execute(context, request)
    assert (context.workspace.output_dir / "saved.md").read_bytes() == b"keep"
    assert (context.workspace.cwd / "source.md").read_bytes() == b"same same"


async def test_concurrent_local_edits_only_one_matching_version_wins(tmp_path):
    write_workspace_file(tmp_path, "workspace/shared.md", b"original")

    def change(value):
        """同じ原版に対する並行 CAS を実 file I/O で試す。"""
        try:
            write_workspace_file(
                tmp_path, "workspace/shared.md", value, expected_hash=checksum(b"original")
            )
            return True
        except UnsafeWorkspaceFileError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = await asyncio.gather(
            *(
                asyncio.get_running_loop().run_in_executor(pool, change, data)
                for data in (b"a", b"b")
            )
        )
    assert sorted(results) == [False, True]


async def test_gateway_returns_only_committed_artifact_refs_for_file_edit(tmp_path):
    """実 Gateway を通し、byte/URI/参照校验と監査 commit 後の交付を確認する。"""
    from dataclasses import replace

    from skillmind.agent.tool_gateway import ToolDefinition, ToolRegistry
    from skillmind.agent.workspace import WorkspaceManager
    from tests.agent.test_gateway_invocation_ownership import OwnershipAuditWriter, _payload
    from tests.agent.test_tool_gateway import CsvIssueProvider, _registry, _schema
    from tests.agent.test_tool_gateway import _context as run_context

    capability = "workspace.edit/v1"
    registry = ToolRegistry(
        (
            ToolDefinition(
                capability=capability,
                description="Edit original files",
                request_schema=_schema(f"tools/{capability}/request.schema.json"),
                response_schema=_schema(f"tools/{capability}/response.schema.json"),
                error_schema=_schema(f"tools/{capability}/error.schema.json"),
                providers={"workspace": WorkspaceEditProvider()},
                unbound_provider="workspace",
                minimum_execution_profile="SUPERVISED",
            ),
        )
    )
    original = run_context(tmp_path, _registry(CsvIssueProvider()))
    tool = registry.resolve(
        capability, provider="workspace", integration_id=None, execution_profile="SUPERVISED"
    )
    workspace = WorkspaceManager(tmp_path / "runs").initialize(original.run_id)
    (workspace.cwd / "source.md").write_bytes(b"original")
    context = replace(
        original,
        workspace=workspace,
        tools=(tool,),
        permission_snapshot={
            "mode": "auto_read_only",
            "allowed_capabilities": [capability],
            "execution_profile": "SUPERVISED",
        },
    )
    writer = OwnershipAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    args = {
        "operation": "append",
        "source_path": "workspace/source.md",
        "path": "output/結果.md",
        "expected_hash": checksum(b"original"),
        "text": "\nResult",
        "purpose": "Append result",
    }
    await runtime.mcp.on_tool_authorized(
        tool.sdk_name, args, "edit-original", "12af775a-d2f9-4fc9-885c-b9fb27bdedcf"
    )
    result = _payload(await runtime.gateway.invoke_mcp(tool.sdk_name, args))
    assert result["status"] == "success"
    record = writer.completed[0][1][0]
    assert result["artifact_refs"] == [record.artifact_ref]
    assert record.draft.artifact.content == b"original\nResult"
