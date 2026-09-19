"""監査 export の保真・帰属・公開・再送の境界を、実 Provider/Gateway で検証する。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote
from uuid import uuid4

import pytest
from skillmind.agent.audit_export import (
    AUDIT_EXPORT_CAPABILITY,
    AuditExportProvider,
    export_selection,
    validate_export_artifact,
)
from skillmind.agent.audit_source import PostgresAuditExportSource, _evidence
from skillmind.agent.context_builder import ContractStore, create_run_tool_registry
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.agent.workspace import WorkspaceManager
from skillmind.artifacts.repository import ArtifactRepository
from skillmind.core.hashing import canonical_json, sha256_hex
from sqlalchemy.dialects.postgresql import dialect
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _context, _registry
from tests.agent.test_workspace_provider import CONTRACTS
from tests.agent.test_workspace_provider import _context as tool_context
from tests.artifacts.test_repository import saved_row, session_for


class MemorySource:
    """外部呼出しを持たない原 Run の監査 fixture。取消と撤権を注入できる。"""

    def __init__(self):
        """入力が推測や正規化を受けないことを確認する原値を保持する。"""
        self.records = {
            "evidence": [{"evidence_ref": "ev_observation", "raw": "hallo\r", "observed": []}],
            "proposals": [
                {
                    "proposal_ref": "cp_operation",
                    "status": "FAILED",
                    "error": {"code": "effect_result_unknown"},
                }
            ],
        }
        self.reads = self.authorizations = 0
        self.revoked_at = None

    async def authorize(self, context):
        """現在権限の確認回数を記録する。"""
        self.authorizations += 1
        if self.revoked_at == self.authorizations:
            raise PermissionError("revoked")

    async def read(self, context, *, evidence_refs, proposal_refs):
        """要求順に全件返し、fixture 本文を変更しない。"""
        self.reads += 1
        return deepcopy(self.records)


def arguments(**overrides):
    """原参照を明示し、model が記録本文を書けない要求を生成する。"""
    return {
        "path": "output/audit.json",
        "purpose": "Export original facts",
        "evidence_refs": ["ev_observation"],
        "proposal_refs": ["cp_operation"],
        **overrides,
    }


def context_at(tmp_path):
    """実 file system と platform capability を持つ Provider context。"""
    context = tool_context(tmp_path, AUDIT_EXPORT_CAPABILITY)
    return replace(context, tool=replace(context.tool, provider="platform"))


async def test_export_preserves_raw_values_unknown_and_does_not_return_full_content(tmp_path):
    """原改行、空観測、UNKNOWN を保持し、本文はモデルへ再送しない。"""
    source = MemorySource()
    context = context_at(tmp_path)
    before = deepcopy(source.records)
    result = await AuditExportProvider(source).execute(context, arguments())
    draft = result.evidence[0]
    assert source.records == before
    assert source.reads == 1 and source.authorizations == 3
    data = (context.workspace.output_dir / "audit.json").read_bytes()
    assert data == draft.artifact.content
    assert result.response["content_hash"] == "sha256:" + sha256_hex(data)
    payload = json.loads(data)
    assert payload["evidence"][0]["raw"] == "hallo\r"
    assert payload["evidence"][0]["observed"] == []
    assert payload["proposals"][0]["error"]["code"] == "effect_result_unknown"
    assert "raw" not in canonical_json(result.response)
    assert "COMPLETED" not in data.decode()
    assert result.response["counts"] == {"evidence": 1, "proposals": 1}


@pytest.mark.parametrize("path", ["input/audit.json", "output/../bad", "/tmp/x", "workspace/x"])
async def test_export_rejects_non_output_and_traversal_before_read(tmp_path, path):
    """副作用前に path を検証し、入力を変更しない。"""
    source = MemorySource()
    with pytest.raises(ToolProviderError):
        await AuditExportProvider(source).execute(context_at(tmp_path), arguments(path=path))
    assert source.reads == 0


@pytest.mark.parametrize(
    "refs", [[], ["ev_a"] * 2, ["foreign"], ["ev_" + str(i) for i in range(51)]]
)
def test_selection_is_exact_bounded_and_nonempty(refs):
    """不正な参照を正規化・省略しない。"""
    with pytest.raises(ValueError):
        export_selection({"evidence_refs": refs})


@pytest.mark.parametrize("change", ["missing", "wrong_ref", "secret", "large"])
async def test_export_fails_before_write_for_unavailable_or_unsafe_source(tmp_path, change):
    """欠落・機密・超限を「一部成功」へ変換しない。"""
    context, source = context_at(tmp_path), MemorySource()
    if change == "missing":
        source.records["evidence"] = []
    elif change == "wrong_ref":
        source.records["evidence"][0]["evidence_ref"] = "ev_foreign"
    elif change == "secret":
        source.records["evidence"][0]["password"] = "fixture-private-value"
    else:
        source.records["evidence"][0]["raw"] = "x" * 1_048_576
    with pytest.raises(ToolProviderError):
        await AuditExportProvider(source).execute(context, arguments())
    assert list(context.workspace.output_dir.iterdir()) == []


@pytest.mark.parametrize("revoked_at", [1, 2, 3])
async def test_revocation_never_returns_publishable_artifact(tmp_path, revoked_at):
    """I/O の前後どちらで撤権しても公開回执を返さない。"""
    source = MemorySource()
    source.revoked_at = revoked_at
    with pytest.raises(ToolProviderError):
        await AuditExportProvider(source).execute(context_at(tmp_path), arguments())


async def test_cancel_propagates_without_output(tmp_path):
    """キャンセルを通常の失敗や成功へ変えない。"""
    context, source = context_at(tmp_path), MemorySource()
    source.read = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await AuditExportProvider(source).execute(context, arguments())
    assert list(context.workspace.output_dir.iterdir()) == []


async def test_gateway_returns_committed_artifact_and_replays_without_new_export(tmp_path):
    """実 registry/gateway/Artifact の validator を通し、原 SDK ID を再使用する。"""
    source = MemorySource()
    catalog = create_run_tool_registry(
        ContractStore(CONTRACTS),
        document_source=SimpleNamespace(),
        audit_export_provider=AuditExportProvider(source),
    )
    exported = catalog.resolve_unbound(AUDIT_EXPORT_CAPABILITY, execution_profile="SUPERVISED")
    context = _context(tmp_path, _registry(CsvIssueProvider()))
    context = replace(
        context,
        tools=(exported,),
        permission_snapshot={
            "mode": "auto_read_only",
            "execution_profile": "SUPERVISED",
            "allowed_capabilities": [AUDIT_EXPORT_CAPABILITY],
        },
        workspace=WorkspaceManager((tmp_path / "runs").resolve()).initialize(context.run_id),
    )
    writer = MemoryAuditWriter()
    runtime = catalog.build_gateway_runtime(context, audit_writer=writer)
    request, session = arguments(), str(uuid4())
    await runtime.mcp.on_tool_authorized(exported.sdk_name, request, "original", session)
    first = await runtime.gateway.invoke_mcp(exported.sdk_name, request)
    assert not first.get("is_error"), first
    response = json.loads(first["content"][0]["text"])
    assert response["artifact_refs"][0].startswith("art_")
    lease = writer.by_use_id["original"]
    records = writer.completed[0][1]
    validate_export_artifact(lease.invocation, result=response, evidence=records)
    await runtime.mcp.on_tool_authorized(exported.sdk_name, request, "original", session)
    second = await runtime.gateway.invoke_mcp(exported.sdk_name, request)
    assert second == first and source.reads == 1


async def test_export_artifact_is_readable_by_existing_repository_and_detects_corruption(tmp_path):
    """文書保存で使う既存 Artifact repository と同じ回执境界を検証する。"""
    context = context_at(tmp_path)
    result = await AuditExportProvider(MemorySource()).execute(context, arguments())
    draft = result.evidence[0]
    row = saved_row(draft.artifact.content)
    row.update(
        run_id=context.run_id,
        tool_run_id=context.run_id,
        attempt_run_id=context.run_id,
        source_uri=f"workspace://runs/{context.run_id}/{quote(row['artifact_path'], safe='/')}",
    )
    row.update(
        capability_version=AUDIT_EXPORT_CAPABILITY,
        provider="platform",
        tool_name="mcp__skillmind__audit_export_v1",
        evidence_type="audit-export",
        source_locator={"path": row["artifact_path"], "bytes": row["artifact_size"]},
        metadata_json=deepcopy(draft.metadata),
    )
    row["result_json"].update(provider="platform", counts={"evidence": 1, "proposals": 1})
    repo = ArtifactRepository(session_for([row]))
    found = await repo.get_content(
        project_id=row["project_id"], run_id=row["run_id"], artifact_ref=row["artifact_ref"]
    )
    assert found.content == draft.artifact.content
    row["content"] = b"tampered"
    with pytest.raises(ValueError):
        await repo.get_content(
            project_id=row["project_id"], run_id=row["run_id"], artifact_ref=row["artifact_ref"]
        )


async def test_source_filters_by_run_and_exact_refs_and_rejects_partial_match():
    """SQL は全履歴取得でなく、Project/Run と明示参照に絞る。実 DB 競争試験ではない。"""
    session = AsyncMock()
    empty = MagicMock()
    empty.all.return_value = []
    session.execute.return_value = empty
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock()
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    source = PostgresAuditExportSource(factory)
    source._authorize = AsyncMock()
    context = SimpleNamespace(run_id=uuid4(), project_id=uuid4())
    with pytest.raises(LookupError):
        await source.read(context, evidence_refs=("ev_one",), proposal_refs=())
    sql = str(session.execute.await_args.args[0].compile(dialect=dialect()))
    assert "evidence.run_id" in sql and "tool_calls.run_id" in sql
    assert "evidence.evidence_ref IN" in sql
    assert "artifact_bytes" not in sql


def test_raw_snapshot_integrity_and_summary_are_not_forged():
    """正本の hash と本文を照合し、読取引数は要約であることを明示する。"""
    now = datetime.now(UTC)
    content = {"status": "TIMEOUT"}
    e = SimpleNamespace(
        evidence_ref="ev_original",
        evidence_type="resource",
        source_uri="mcp://test",
        source_locator={},
        content_hash="sha256:" + sha256_hex(canonical_json(content)),
        snapshot_uri=None,
        excerpt=None,
        metadata_json={"snapshot": content},
        artifact_ref=None,
        created_at=now,
    )
    c = SimpleNamespace(
        id=uuid4(),
        tool_name="generic",
        capability_version="mcp.query/v1",
        provider="mcp",
        status="SUCCEEDED",
        arguments_summary={"keys": ["name"]},
        request_fingerprint="x",
        result_json={"value": "raw\r"},
        error_json=None,
        duration_ms=1,
        created_at=now,
        updated_at=now,
    )
    projected = _evidence(e, c)
    assert projected["tool_call"]["arguments_summary"] == {"keys": ["name"]}
    assert "arguments" not in projected["tool_call"]
    assert projected["metadata_json"]["snapshot"]["status"] == "TIMEOUT"
    e.metadata_json["snapshot"] = {"status": "COMPLETED"}
    with pytest.raises(ValueError):
        _evidence(e, c)


def test_export_catalog_keeps_interpreter_identity_loadable():
    """新 Tool を追加しても catalog の旧 checksum で解釈器を利用不能にしない。"""
    from pathlib import Path

    from skillmind.skills.interpreter import load_capability_catalog

    catalog = load_capability_catalog(
        Path(__file__).resolve().parents[3] / "contracts/examples/skill-capability-catalog.v1.json"
    )
    entries = [item for item in catalog.capabilities if item.capability == "audit.export/v1"]
    assert len(entries) == 1
    assert entries[0].providers == ("platform",)
    assert entries[0].request_schema == "tools/audit.export/v1/request.schema.json"
