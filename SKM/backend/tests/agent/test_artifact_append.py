"""原 byte 連結から Gateway 公開・Artifact 再読までを隔離 fixture で検証する。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects.postgresql import dialect

from skillmind.agent.artifact_append import (
    CAPABILITY,
    ArtifactAppendProvider,
    validate_append_publication,
)
from skillmind.agent.audit_source import PostgresAuditExportSource
from skillmind.agent.contract_store import ContractStore
from skillmind.agent.tool_catalog import create_run_tool_registry
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.agent.workspace import WorkspaceManager
from skillmind.artifacts.domain import MAX_ARTIFACT_BYTES, ArtifactContent
from skillmind.core.hashing import canonical_json
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _context, _registry
from tests.agent.test_workspace_provider import CONTRACTS
from tests.agent.test_workspace_provider import _context as tool_context
from tests.artifacts.test_domain import metadata
from tests.artifacts.test_repository import read, saved_row, session_for


class MemorySource:
    """DB 公開済みの不変 byte を模し、撤権と境界違反を注入する。"""

    def __init__(self, context, data=b"original\r\n"):
        """参照だけ存在し、初期 workspace には file がない。"""
        self.original = ArtifactContent(
            replace(
                metadata(data),
                project_id=context.project_id,
                run_id=context.run_id,
                path="output/source.md",
            ),
            data,
        )
        self.reads = self.authorizations = 0
        self.revoked_at = None

    async def authorize(self, context):
        """権限変更を I/O 前後に発生させる。"""
        self.authorizations += 1
        if self.revoked_at == self.authorizations:
            raise PermissionError("private diagnostic")

    async def read_artifact(self, context, artifact_ref):
        """原参照を検証し、可変 workspace は読まない。"""
        self.reads += 1
        assert artifact_ref == self.original.metadata.artifact_ref
        return self.original


def context_at(tmp_path):
    """platform capability の実 filesystem context を作る。"""
    context = tool_context(tmp_path, CAPABILITY)
    return replace(context, tool=replace(context.tool, provider="platform"))


def arguments(**overrides):
    """全文でなく短い付記と原参照を送る。改行も model が指定する。"""
    return {
        "artifact_ref": "art_original",
        "text": "\n\n## 付記\n確認済み。\n",
        "purpose": "Append reviewed scope",
        **overrides,
    }


async def test_large_logical_artifact_overwrites_same_path_exactly_and_retry_does_not_double(
    tmp_path,
):
    """大きな変換成果と CRLF/書式を保ち、同原参照の再実行も二重追加しない。"""
    context = context_at(tmp_path)
    original = ("| ~~旧~~ | 新 |\r\n" * 12000).encode()
    source = MemorySource(context, original)
    provider = ArtifactAppendProvider(source)
    suffix = arguments()["text"].encode()
    assert not (context.workspace.output_dir / "source.md").exists()
    first = await provider.execute(context, arguments())
    assert first.response["created"] is True
    second = await provider.execute(context, arguments())
    assert second.response["created"] is False
    assert (
        first.evidence[0].artifact.content
        == second.evidence[0].artifact.content
        == original + suffix
    )
    assert (context.workspace.output_dir / "source.md").read_bytes() == original + suffix
    assert list(context.workspace.output_dir.iterdir()) == [
        context.workspace.output_dir / "source.md"
    ]
    assert source.original.content == original
    assert len(canonical_json(first.response)) < 1000
    assert "確認済み" not in canonical_json(first.response)
    assert source.reads == 2 and source.authorizations == 6


@pytest.mark.parametrize("change", ["run", "project", "ref", "missing", "corrupt"])
async def test_foreign_missing_or_corrupt_artifact_never_writes_or_exposes_source(tmp_path, change):
    """他 Run/Project と読めない原文を同じ固定診断で拒否する。"""
    context = context_at(tmp_path)
    source = MemorySource(context)
    if change in {"missing", "corrupt"}:
        error = LookupError if change == "missing" else ValueError
        source.read_artifact = AsyncMock(side_effect=error("private diagnostic"))
    else:
        key = {"run": "run_id", "project": "project_id", "ref": "artifact_ref"}[change]
        value = "art_foreign" if change == "ref" else uuid4()
        source.original = replace(
            source.original, metadata=replace(source.original.metadata, **{key: value})
        )
        source.read_artifact = AsyncMock(return_value=source.original)
    with pytest.raises(ToolProviderError) as error:
        await ArtifactAppendProvider(source).execute(context, arguments())
    assert error.value.code == "not_found" and "private" not in str(error.value)
    assert list(context.workspace.output_dir.iterdir()) == []


@pytest.mark.parametrize("text", ["", "x" * 16385, "\ud800", None])
async def test_invalid_suffix_is_rejected_before_read(tmp_path, text):
    """空・過大・非 UTF-8 の追加文を原文アクセス前に拒否する。"""
    context = context_at(tmp_path)
    source = MemorySource(context)
    with pytest.raises(ToolProviderError) as error:
        await ArtifactAppendProvider(source).execute(context, arguments(text=text))
    assert error.value.code == "invalid_request" and source.reads == 0


async def test_size_limit_preserves_existing_output(tmp_path):
    """上限超過時は部分上書きせず、既存出力を保持する。"""
    context = context_at(tmp_path)
    target = context.workspace.output_dir / "source.md"
    target.write_bytes(b"existing")
    source = MemorySource(context, b"a" * MAX_ARTIFACT_BYTES)
    with pytest.raises(ToolProviderError) as error:
        await ArtifactAppendProvider(source).execute(context, arguments(text="b"))
    assert error.value.code == "too_large"
    assert target.read_bytes() == b"existing"


@pytest.mark.parametrize("revoked_at", [1, 2, 3])
async def test_revocation_never_publishes(tmp_path, revoked_at):
    """書込後も再認可し、撤権時に Artifact の公開を成功にしない。"""
    context = context_at(tmp_path)
    source = MemorySource(context)
    source.revoked_at = revoked_at
    with pytest.raises(ToolProviderError):
        await ArtifactAppendProvider(source).execute(context, arguments())


async def test_cancel_propagates_and_symlink_cannot_overwrite_input(tmp_path):
    """取消を通常失敗へ変換せず、既存の安全な path 検証を使う。"""
    context = context_at(tmp_path)
    source = MemorySource(context)
    source.read_artifact = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ArtifactAppendProvider(source).execute(context, arguments())
    outside = tmp_path / "protected.md"
    outside.write_bytes(b"protected")
    (context.workspace.output_dir / "source.md").symlink_to(outside)
    with pytest.raises(ToolProviderError):
        await ArtifactAppendProvider(MemorySource(context)).execute(context, arguments())
    assert outside.read_bytes() == b"protected"


async def test_gateway_publication_replay_and_exact_suffix_validation(tmp_path):
    """実 tool 契約・監査・公開を通し、SDK 再送で新しい書込を作らない。"""
    context = _context(tmp_path, _registry(CsvIssueProvider()))
    source = MemorySource(context)
    catalog = create_run_tool_registry(
        ContractStore(CONTRACTS),
        document_source=SimpleNamespace(),
        artifact_append_provider=ArtifactAppendProvider(source),
    )
    tool = catalog.resolve_unbound(CAPABILITY, execution_profile="SUPERVISED")
    context = replace(
        context,
        tools=(tool,),
        permission_snapshot={
            "mode": "auto_read_only",
            "execution_profile": "SUPERVISED",
            "allowed_capabilities": [CAPABILITY],
        },
        workspace=WorkspaceManager((tmp_path / "runs").resolve()).initialize(context.run_id),
    )
    writer = MemoryAuditWriter()
    runtime = catalog.build_gateway_runtime(context, audit_writer=writer)
    request, session = arguments(), str(uuid4())
    await runtime.mcp.on_tool_authorized(tool.sdk_name, request, "original", session)
    first = await runtime.gateway.invoke_mcp(tool.sdk_name, request)
    assert not first.get("is_error"), first
    response = json.loads(first["content"][0]["text"])
    assert response["status"] == "success", response
    assert response["artifact_refs"][0] != "art_original"
    invocation = writer.by_use_id["original"].invocation
    records = writer.completed[0][1]
    validate_append_publication(invocation, result=response, evidence=records)
    with pytest.raises(ValueError):
        validate_append_publication(
            replace(invocation, arguments=arguments(text="different")),
            result=response,
            evidence=records,
        )
    await runtime.mcp.on_tool_authorized(tool.sdk_name, request, "original", session)
    assert await runtime.gateway.invoke_mcp(tool.sdk_name, request) == first
    assert source.reads == 1


async def append_row(tmp_path):
    """実 Provider の成果を既存 repository の保存 mapping に接続する。"""
    context = context_at(tmp_path)
    result = await ArtifactAppendProvider(MemorySource(context)).execute(context, arguments())
    draft = result.evidence[0]
    row = saved_row(draft.artifact.content)
    row.update(
        run_id=context.run_id,
        tool_run_id=context.run_id,
        attempt_run_id=context.run_id,
        artifact_path=draft.artifact.path,
        source_uri=draft.source_uri,
        capability_version=CAPABILITY,
        provider="platform",
        tool_name="mcp__skillmind__artifact_append_v1",
        evidence_type="artifact-append",
        source_locator=deepcopy(draft.source_locator),
        metadata_json=deepcopy(draft.metadata),
    )
    row["result_json"].update(result.response)
    return row


@pytest.mark.parametrize("action", ["list", "get", "refs"])
async def test_appended_artifact_can_be_listed_downloaded_and_used_as_verified_ref(
    tmp_path, action
):
    """文書保存/結果検証も使う repository が新 producer の確定参照を認める。"""
    row = await append_row(tmp_path)
    assert await read(action, session_for([row]), row) is not None
    if action == "get":
        found = await read(action, session_for([row]), row)
        (tmp_path / "next").mkdir()
        context = context_at(tmp_path / "next")
        source = MemorySource(context)
        source.original = replace(
            found,
            metadata=replace(
                found.metadata,
                run_id=context.run_id,
                project_id=context.project_id,
            ),
        )
        chained = await ArtifactAppendProvider(source).execute(context, arguments(text="next"))
        assert chained.evidence[0].artifact.content == found.content + b"next"


@pytest.mark.parametrize("action", ["list", "get", "refs"])
@pytest.mark.parametrize(
    "key,value",
    [
        ("source_bytes", True),
        ("source_bytes", -1),
        ("source_artifact_ref", "foreign"),
        ("append_hash", "invalid"),
        ("appended_bytes", 1),
    ],
)
async def test_invalid_proof_is_rejected_even_when_response_agrees(tmp_path, action, key, value):
    """本文なしの一覧でも壊れた原 byte 帰属を成功にしない。"""
    row = await append_row(tmp_path)
    row["metadata_json"][key] = row["result_json"][key] = value
    with pytest.raises(ValueError):
        await read(action, session_for([row]), row)


async def test_original_prefix_hash_tampering_is_detected_on_read(tmp_path):
    """外形と最終 hash が正しくても、原文/付記の分割 hash を照合する。"""
    row = await append_row(tmp_path)
    row["metadata_json"]["source_content_hash"] = row["result_json"]["source_content_hash"] = (
        "sha256:" + "0" * 64
    )
    with pytest.raises(ValueError):
        await read("get", session_for([row]), row)


async def test_postgres_source_authorizes_then_queries_exact_project_run_and_ref(tmp_path):
    """source は現在認可を通し、SQL に三つの scope を必須にする。"""
    row = saved_row()
    context = SimpleNamespace(run_id=row["run_id"], project_id=row["project_id"])
    session = session_for([row])
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock()
    source = PostgresAuditExportSource(factory)
    source._authorize = AsyncMock()
    found = await source.read_artifact(context, row["artifact_ref"])
    assert found.content == row["content"]
    source._authorize.assert_awaited_once_with(session, context)
    compiled = session.execute.await_args.args[0].compile(dialect=dialect())
    assert all(
        value in compiled.params.values()
        for value in (
            row["run_id"],
            row["project_id"],
            row["artifact_ref"],
        )
    )
