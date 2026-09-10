"""実 workspace Producer/Gateway と Tool transaction の Artifact 発行境界を検証する。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from projectmind.agent.evidence import EvidenceRecord, ToolInvocation, invocation_fingerprint
from projectmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    RunToolRuntime,
    ToolDefinition,
    ToolRegistry,
)
from projectmind.agent.workspace import WorkspaceManager
from projectmind.agent.workspace_provider import WorkspaceWriteProvider
from projectmind.artifacts.domain import ArtifactDraft, ArtifactMetadata
from projectmind.db.models import Evidence, ToolCall
from projectmind.runs.domain import LeaseValidationError, RunCancellationRequestedError
from tests.agent.test_gateway_invocation_ownership import OwnershipAuditWriter, _payload
from tests.agent.test_tool_audit_transactions import AuditDatabase
from tests.agent.test_tool_gateway import CsvIssueProvider, _context, _registry, _schema
from tests.agent.test_workspace_provider import _context as workspace_context


@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_invalid_audit_locator_is_rejected_before_file_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str,
) -> None:
    """URI の保存上限など既知の拒否を、書込後の孤立 file へ変換しない。"""

    context = workspace_context(tmp_path, f"workspace.write/{version}")
    write = MagicMock()
    monkeypatch.setattr("projectmind.agent.workspace_provider._write_workspace_file", write)
    path = "output/" + "/".join(["文" * 50] * 5) + "/report.txt"
    with pytest.raises(ValueError, match="source_uri"):
        await WorkspaceWriteProvider().execute(
            context, {"path": path, "content": "Report", "purpose": "Produce"},
        )
    write.assert_not_called()


class ObservedWriteProvider(WorkspaceWriteProvider):
    """実 file 書込の回数と、候補生成直後の境界違反を観測する。"""

    def __init__(self, corruption: str | None = None) -> None:
        """固定した合成失敗だけを注入し、他の I/O は本番 Provider に委譲する。"""

        self.calls = 0
        self.corruption = corruption

    async def execute(
        self,
        context: RunToolContext,
        arguments: Mapping[str, Any],
    ) -> ProviderToolResult:
        """byte の発行元は元 Provider のまま、公開 ref 自称や snapshot 差替えを試みる。"""

        self.calls += 1
        value = await super().execute(context, arguments)
        response = dict(value.response)
        draft = value.evidence[0]
        if self.corruption == "assigned_ref":
            response["artifact_refs"] = [f"art_{uuid4().hex}"]
        elif self.corruption == "missing_snapshot":
            draft = replace(draft, artifact=None)
        elif self.corruption == "different_path":
            assert draft.artifact is not None
            draft = replace(draft, artifact=replace(draft.artifact, path="output/other.txt"))
        elif self.corruption == "unbound_v1":
            draft = replace(
                draft,
                artifact=ArtifactDraft(
                    path="output/report.txt",
                    content=arguments["content"].encode("utf-8"),
                ),
            )
        return ProviderToolResult(response=response, evidence=(draft,))


def _runtime(
    tmp_path: Path,
    writer: OwnershipAuditWriter,
    provider: ObservedWriteProvider,
    *,
    version: str = "v2",
) -> tuple[RunToolRuntime, str]:
    """本物の versioned request/response と明示権限で MCP 経路を構築する。"""

    capability = f"workspace.write/{version}"
    registry = ToolRegistry(
        (
            ToolDefinition(
                capability=capability,
                description="Write bounded Run files",
                request_schema=_schema(f"tools/workspace.write/{version}/request.schema.json"),
                response_schema=_schema(f"tools/workspace.write/{version}/response.schema.json"),
                error_schema=_schema(f"tools/workspace.write/{version}/error.schema.json"),
                providers={"workspace": provider},
                unbound_provider="workspace",
                minimum_execution_profile="SUPERVISED",
            ),
        )
    )
    original = _context(tmp_path, _registry(CsvIssueProvider()))
    tool = registry.resolve(
        capability, provider="workspace", integration_id=None, execution_profile="SUPERVISED",
    )
    context = replace(
        original,
        workspace=WorkspaceManager((tmp_path / "runs").resolve()).initialize(original.run_id),
        tools=(tool,),
        permission_snapshot={
            "mode": "auto_read_only",
            "allowed_capabilities": [capability],
            "execution_profile": "SUPERVISED",
        },
    )
    return registry.build_gateway_runtime(context, audit_writer=writer), tool.sdk_name


async def _invoke(
    runtime: RunToolRuntime,
    name: str,
    *,
    content: str = "Original report 日本語",
    path: str = "output/report.txt",
    use_id: str = "artifact-original",
) -> dict[str, Any]:
    """同一 SDK session 内の精確 Tool use identity で許可と handler を結ぶ。"""

    arguments = {"path": path, "content": content, "purpose": "Save the report"}
    assert runtime.mcp.on_tool_authorized is not None
    await runtime.mcp.on_tool_authorized(
        name,
        arguments,
        use_id,
        "12af775a-d2f9-4fc9-885c-b9fb27bdedcf",
    )
    return await runtime.gateway.invoke_mcp(name, arguments)


@pytest.mark.parametrize("version,root", [("v1", "output"), ("v2", "workspace"), ("v2", "output")])
async def test_only_v2_output_publishes_a_snapshot(tmp_path: Path, version: str, root: str) -> None:
    """旧 v1 と中間 file の意味を保ち、output v2 にだけ commit 後の ref を返す。"""

    writer, provider = OwnershipAuditWriter(), ObservedWriteProvider()
    runtime, name = _runtime(tmp_path, writer, provider, version=version)
    first = _payload(await _invoke(runtime, name, path=f"{root}/report.txt"))
    assert first["status"] == "success"
    assert len(writer.completed) == 1
    draft = writer.completed[0][1][0]
    if version == "v1":
        assert "artifact_refs" not in first and draft.artifact_ref is None
        assert draft.draft.artifact is None
    elif root == "workspace":
        assert first["artifact_refs"] == [] and draft.draft.artifact is None
    else:
        assert first["artifact_refs"] == [draft.artifact_ref]
        assert draft.draft.artifact is not None
        assert draft.draft.artifact.content == "Original report 日本語".encode()
        assert "artifact_bytes" not in json.dumps(first)
        assert "Original report" not in json.dumps(first)
    second = _payload(await _invoke(runtime, name, path=f"{root}/report.txt"))
    assert first == second and provider.calls == 1


async def test_same_path_new_write_keeps_original_snapshot_and_issues_new_identity(
    tmp_path: Path,
) -> None:
    """file の上書きと不変 Artifact を区別し、同 path の各発行を独立保存する。"""

    writer, provider = OwnershipAuditWriter(), ObservedWriteProvider()
    runtime, name = _runtime(tmp_path, writer, provider)
    first = _payload(await _invoke(runtime, name))
    second = _payload(await _invoke(runtime, name, content="Replacement", use_id="second-write"))
    assert first["artifact_refs"] != second["artifact_refs"]
    snapshots = [entry[1][0].draft.artifact for entry in writer.completed]
    assert snapshots[0] is not None and snapshots[1] is not None
    assert snapshots[0].content == "Original report 日本語".encode()
    assert snapshots[1].content == b"Replacement"
    assert second["created"] is False


@pytest.mark.parametrize(
    "corruption", ["assigned_ref", "missing_snapshot", "different_path", "unbound_v1"]
)
async def test_provider_cannot_forge_or_omit_required_publication(
    tmp_path: Path, corruption: str
) -> None:
    """Provider 自称 ref、不一致 snapshot と v1 の隠れた発行を監査成功より前に拒否する。"""

    writer, provider = OwnershipAuditWriter(), ObservedWriteProvider(corruption)
    runtime, name = _runtime(
        tmp_path, writer, provider, version="v1" if corruption == "unbound_v1" else "v2"
    )
    reply = await _invoke(runtime, name)
    assert reply["is_error"] is True and _payload(reply)["code"] == "unavailable"
    assert not writer.completed and len(writer.failed) == 1


async def test_no_artifact_is_returned_before_commit_confirmation(tmp_path: Path) -> None:
    """候補 byte があっても監査の完了応答までは Agent に art_ を交付しない。"""

    writer, provider = OwnershipAuditWriter(pause="complete"), ObservedWriteProvider()
    runtime, name = _runtime(tmp_path, writer, provider)
    task = asyncio.create_task(_invoke(runtime, name))
    try:
        await asyncio.wait_for(writer.entered.wait(), 2)
        assert not task.done() and not writer.saved
    finally:
        writer.release.set()
    reply = _payload(await asyncio.wait_for(task, 2))
    assert reply["artifact_refs"] == writer.saved[0]["artifact_refs"]


async def test_commit_unknown_returns_no_ref_and_never_reruns_provider(tmp_path: Path) -> None:
    """commit 済みの応答喪失は補償失敗にせず、原成功照会で同じ ref を再読取する。"""

    writer, provider = OwnershipAuditWriter(), ObservedWriteProvider()
    writer.commit_response_lost = True
    runtime, name = _runtime(tmp_path, writer, provider)
    first = await _invoke(runtime, name)
    assert first["is_error"] is True and "artifact_refs" not in _payload(first)
    assert not writer.failed
    second = _payload(await _invoke(runtime, name))
    assert second["artifact_refs"] == writer.saved[0]["artifact_refs"] and provider.calls == 1


async def _publication(
    database: AuditDatabase, tmp_path: Path
) -> tuple[dict[str, Any], EvidenceRecord]:
    """実 Producer の出力を、原 claim/ToolInvocation と採番済み response に結び付ける。"""

    context = workspace_context(tmp_path, "workspace.write/v2")
    context = replace(
        context,
        run_id=database.claimed.run_id,
        run_attempt_id=database.claimed.run_attempt_id,
        project_id=database.claimed.project_id,
        user_id=database.claimed.actor_id,
    )
    arguments = {"path": "output/report.txt", "content": "Report 日本語", "purpose": "Produce"}
    database.invocation = ToolInvocation(
        run_id=context.run_id,
        run_attempt_id=context.run_attempt_id,
        agent_session_id=uuid4(),
        sdk_tool_use_id="artifact-db-original",
        tool=context.tool,
        arguments=arguments,
        request_fingerprint=invocation_fingerprint(context.tool.sdk_name, arguments),
    )
    produced = await WorkspaceWriteProvider().execute(context, arguments)
    record = EvidenceRecord(f"ev_{uuid4().hex}", produced.evidence[0], f"art_{uuid4().hex}")
    return {
        **produced.response,
        "evidence_refs": [record.evidence_ref],
        "artifact_refs": [record.artifact_ref],
    }, record


async def test_real_writer_commits_private_bytes_with_same_tool_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """一つの writer transaction だけが original byte と Tool SUCCEEDED を同時に確定する。"""

    database = AuditDatabase(monkeypatch)
    result, record = await _publication(database, tmp_path)
    quota = AsyncMock(return_value=())
    monkeypatch.setattr("projectmind.artifacts.repository.ArtifactRepository.list_metadata", quota)
    lease = await database.start()
    reply = await database.writer.complete(lease, result=result, evidence=(record,), duration_ms=1)
    saved = database.rows(Evidence)[0]
    assert saved.artifact_ref == record.artifact_ref
    assert saved.artifact_bytes == "Report 日本語".encode()
    assert saved.artifact_size == len(saved.artifact_bytes)
    assert saved.artifact_path == "output/report.txt" and saved.artifact_mime_type == "text/plain"
    assert "artifact_bytes" not in saved.metadata_json
    assert database.rows(ToolCall)[0].result_json == reply == result
    assert database.timeline[-1] == "commit"
    quota.assert_awaited_once_with(
        project_id=database.claimed.project_id, run_id=database.claimed.run_id
    )


@pytest.mark.parametrize("failure", ["cancel", "expiry", "scope_close", "flush", "before", "after"])
async def test_failed_or_unknown_publication_does_not_invent_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    """quota 待機後の fencing、flush/commit 失敗を区別し、未知時も保存回执を補作しない。"""

    database = AuditDatabase(monkeypatch)
    result, record = await _publication(database, tmp_path)
    lease = await database.start()

    async def quota(_repository: object, **_arguments: Any) -> tuple[ArtifactMetadata, ...]:
        """Run lock の後の query 待機中に時刻/取消を変える。"""

        database._inject(failure)
        return ()

    monkeypatch.setattr("projectmind.artifacts.repository.ArtifactRepository.list_metadata", quota)
    if failure == "flush":
        database.on_flush = "error"
    elif failure in {"before", "after"}:
        database.commit_failure = failure
    with pytest.raises((ConnectionError, LeaseValidationError, RunCancellationRequestedError)):
        await database.writer.complete(lease, result=result, evidence=(record,), duration_ms=1)
    if failure == "after":
        assert database.rows(Evidence)[0].artifact_ref == record.artifact_ref
        assert database.rows(ToolCall)[0].status == "SUCCEEDED"
    else:
        assert not database.rows(Evidence) and database.rows(ToolCall)[0].status == "RUNNING"


@pytest.mark.parametrize("count,size", [(100, 0), (10, 1_048_576)])
async def test_run_quota_rejects_new_snapshot_without_deleting_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    count: int,
    size: int,
) -> None:
    """file 数と実保存 byte のどちらの上限も Run lock 内で拒否し、既存占用を保持する。"""

    database = AuditDatabase(monkeypatch)
    result, record = await _publication(database, tmp_path)
    saved = tuple(
        ArtifactMetadata(
            artifact_ref=f"art_{uuid4().hex}",
            evidence_ref=f"ev_{uuid4().hex}",
            project_id=database.claimed.project_id,
            run_id=database.claimed.run_id,
            tool_call_id=uuid4(),
            path="output/existing.txt",
            size_bytes=size,
            mime_type="text/plain",
            checksum="sha256:" + "a" * 64,
            created_at=database.now,
        )
        for _ in range(count)
    )
    quota = AsyncMock(return_value=saved)
    monkeypatch.setattr("projectmind.artifacts.repository.ArtifactRepository.list_metadata", quota)
    lease = await database.start()
    with pytest.raises(ValueError, match="storage limit"):
        await database.writer.complete(lease, result=result, evidence=(record,), duration_ms=1)
    assert not database.rows(Evidence) and database.rows(ToolCall)[0].status == "RUNNING"
    assert len(saved) == count
