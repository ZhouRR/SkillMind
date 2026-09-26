"""実 converter→Gateway→audit→Artifact 読取を接続する。DB transaction は局部 fake。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from skillmind.agent.contract_store import (
    ContractStore,
)
from skillmind.agent.document_provider import DocumentConvertProvider
from skillmind.agent.tool_catalog import (
    document_convert_tool_definition,
)
from skillmind.agent.tool_gateway import ToolRegistry
from skillmind.artifacts.domain import ArtifactIntegrityError
from skillmind.artifacts.repository import ArtifactRepository
from skillmind.core.hashing import sha256_hex
from skillmind.db.models import Evidence, ToolCall
from tests.agent.test_document_provider import CONTRACTS, _context, _FakeSource
from tests.agent.test_gateway_invocation_ownership import _payload
from tests.agent.test_runtime_context import _document_manifest, _generic_claimed
from tests.artifacts.test_publication_pipeline import ArtifactDatabase, _result
from tests.documents.fakes import document_content

CAPABILITY = "document.convert/v1"


def case(monkeypatch, *, source_key=None, workspace=None):
    """合成 XLS を、実 Tool/Audit と元の Project/Run/Attempt に接続する。"""
    db = ArtifactDatabase(monkeypatch)
    manifest = _document_manifest()
    # 既存 fixture の凍結宣言を変換能力に置換し、正規 checksum の生成は共用 helper に任せる。
    manifest = json.loads(json.dumps(manifest).replace("document.read/v1", CAPABILITY))
    frozen = _generic_claimed(
        manifest=manifest,
        allowed=(CAPABILITY,), selected_sources={},
    )
    db.run.task_snapshot_json = {
        **frozen.task_snapshot_json, "skill_snapshots": list(frozen.skill_snapshots_json),
    }
    data = (Path(__file__).parents[1] / "fixtures/documents/conversion-sample.xls").read_bytes()
    content = replace(document_content(data, name="cases.xls"), source_object_key=source_key)
    original = _context(db.claimed.project_id, content=content).run
    source = _FakeSource(project_id=db.claimed.project_id, content=content)
    definition = document_convert_tool_definition(ContractStore(CONTRACTS), source)
    registry = ToolRegistry([definition])
    tool = registry.resolve(CAPABILITY, provider="project-documents", integration_id=None)
    run = replace(
        original, run_id=db.claimed.run_id, run_attempt_id=db.claimed.run_attempt_id,
        user_id=db.claimed.actor_id, tools=(tool,),
        permission_snapshot={"allowed_capabilities": [CAPABILITY]},
        resolved_sources={key: {**value, "capability": CAPABILITY}
                          for key, value in original.resolved_sources.items()},
    )
    if workspace is not None:
        run = replace(run, workspace=workspace, permission_snapshot={
            "allowed_capabilities": [CAPABILITY, "workspace.read/v1"],
        })
    runtime = registry.build_gateway_runtime(run, audit_writer=db.writer)
    return db, runtime, tool, source


async def invoke(runtime, tool, *, publish=True, use_id="convert-original", response_mode=None):
    """同じ SDK use ID は原結果を読み、別 use は別の変換として処理する。"""
    args = {"path": "specs/cases.xls", "purpose": "Back up exact Markdown"}
    if response_mode is not None:
        args["response_mode"] = response_mode
    if publish is not None:
        args["publish_artifact"] = publish
    await runtime.mcp.on_tool_authorized(
        tool.sdk_name, args, use_id, "00000000-0000-4000-8000-000000000aaa",
    )
    return await runtime.gateway.invoke_mcp(tool.sdk_name, args)


@pytest.mark.parametrize("source_key", [None, "original/source/cases.xls"])
async def test_native_markdown_is_published_once_and_read_as_original_artifact(
    monkeypatch, source_key
):
    """全文の byte/hash・日本語・原証拠を保ち、成功重放は取得/変換を再実行しない。"""
    db, runtime, tool, source = case(monkeypatch, source_key=source_key)
    reply = await invoke(runtime, tool)
    assert not reply.get("is_error"), reply
    response = _payload(reply)
    artifact_ref, = response["artifact_refs"]
    repository = ArtifactRepository(db())
    saved = await repository.get_content(
        project_id=db.claimed.project_id, run_id=db.claimed.run_id, artifact_ref=artifact_ref,
    )
    assert saved is not None
    assert saved.content == response["markdown"].encode("utf-8")
    assert "正常終了" in saved.content.decode("utf-8")
    assert saved.metadata.checksum == response["markdown_checksum"]
    assert saved.metadata.size_bytes == response["artifact"]["size_bytes"]
    assert response["markdown_checksum"] == "sha256:" + sha256_hex(saved.content)
    assert len(await repository.list_metadata(
        project_id=db.claimed.project_id, run_id=db.claimed.run_id,
    )) == 1
    validated = await _result(db, artifact_ref)
    assert validated.artifact_refs == frozenset({artifact_ref})
    assert _payload(await invoke(runtime, tool)) == response
    assert len(source.calls) == 1 and len(db.rows(Evidence)) == 2
    assert db.rows(Evidence)[0].artifact_bytes is None
    assert db.rows(Evidence)[0].content_hash == response["document"]["checksum"]
    if source_key is not None:
        assert response["source_object_key"] == source_key
        assert db.rows(Evidence)[0].metadata_json["source_object_key"] == source_key


@pytest.mark.parametrize("publish", [None, False])
async def test_default_conversion_does_not_publish_or_consume_artifact_quota(monkeypatch, publish):
    """旧呼出形状は原 Evidence 一件のみで、内部保存を暗黙に追加しない。"""
    db, runtime, tool, _source = case(monkeypatch)
    reply = await invoke(runtime, tool, publish=publish)
    assert not reply.get("is_error"), reply
    response = _payload(reply)
    assert "artifact" not in response and "artifact_refs" not in response
    assert len(db.rows(Evidence)) == 1 and db.rows(Evidence)[0].artifact_ref is None


@pytest.mark.parametrize("corruption", ["bytes", "response", "provider", "source", "refs"])
async def test_conversion_artifact_corruption_blocks_read_and_replay(monkeypatch, corruption):
    """保存 byte・原 Tool・来歴/参照の不整合を修復して別成果として再交付しない。"""
    db, runtime, tool, source = case(monkeypatch)
    first = _payload(await invoke(runtime, tool))
    evidence, call = db.rows(Evidence)[1], db.rows(ToolCall)[0]
    if corruption == "bytes":
        evidence.artifact_bytes = b"changed"
    elif corruption == "response":
        call.result_json["markdown"] = "changed"
    elif corruption == "provider":
        call.provider = "workspace"
    elif corruption == "source":
        evidence.source_locator["source_checksum"] = "sha256:" + "b" * 64
    else:
        call.result_json["artifact_refs"] = []
    with pytest.raises(ArtifactIntegrityError):
        await ArtifactRepository(db()).get_content(
            project_id=db.claimed.project_id, run_id=db.claimed.run_id,
            artifact_ref=first["artifact_refs"][0],
        )
    if corruption == "provider":
        with pytest.raises(ValueError, match="original invocation"):
            await invoke(runtime, tool)
    else:
        assert (await invoke(runtime, tool)).get("is_error") is True
    assert len(source.calls) == 1


@pytest.mark.parametrize("corruption", ["omit", "unrequested", "size", "markdown"])
async def test_conversion_publication_must_match_explicit_request_and_exact_bytes(
    monkeypatch, corruption,
):
    """Provider の不完全候補や暗黙保存を、Gateway と成功 transaction の前で拒否する。"""
    original = DocumentConvertProvider.execute

    async def corrupted(self, context, arguments):
        """native 変換後の候補だけを変更し、実 file 取得/変換の検証は省略しない。"""
        result = await original(self, context, {**arguments, "publish_artifact": True})
        if corruption == "omit":
            return replace(result, evidence=result.evidence[:1])
        response = dict(result.response)
        if corruption == "size":
            response["artifact"] = {**response["artifact"], "size_bytes": 1}
        elif corruption == "markdown":
            response["markdown"] += "changed"
        return replace(result, response=response)

    monkeypatch.setattr(DocumentConvertProvider, "execute", corrupted)
    db, runtime, tool, _source = case(monkeypatch)
    reply = await invoke(runtime, tool, publish=corruption != "unrequested")
    assert reply.get("is_error") is True
    assert "artifact_refs" not in _payload(reply)
    assert not db.rows(Evidence) and db.rows(ToolCall)[0].status == "FAILED"


@pytest.mark.parametrize("failure", ["before", "after"])
async def test_conversion_commit_loss_does_not_deliver_unconfirmed_artifact(monkeypatch, failure):
    """audit 完了 commit のみを故障させ、原変換の再実行や候補 ref の公開を防ぐ。"""
    db, runtime, tool, source = case(monkeypatch)
    original = db.writer.complete

    async def fail_commit(*args, **kwargs):
        """呼出許可の commit は通し、成果保存の一回の commit だけを失敗させる。"""
        db.commit_failure = failure
        return await original(*args, **kwargs)

    monkeypatch.setattr(db.writer, "complete", fail_commit)
    reply = await invoke(runtime, tool)
    assert reply.get("is_error") is True and "artifact_refs" not in _payload(reply)
    monkeypatch.setattr(db.writer, "complete", original)
    replay = await invoke(runtime, tool)
    if failure == "after":
        assert _payload(replay)["artifact_refs"] == (
            db.rows(ToolCall)[0].result_json["artifact_refs"]
        )
    else:
        assert replay.get("is_error") is True and not db.rows(Evidence)
    assert len(source.calls) == 1


async def test_conversion_append_persist_and_result_use_the_same_verified_bytes(
    monkeypatch, tmp_path,
):
    """実変換→追記→監査 commit→結果引用を接続し、file 不在から全文転記なしで保存する。"""
    from unittest.mock import AsyncMock

    from skillmind.agent.artifact_append import ArtifactAppendProvider
    from skillmind.agent.audit_source import PostgresAuditExportSource
    from skillmind.agent.tool_catalog import create_run_tool_registry
    from skillmind.agent.workspace import WorkspaceManager

    db, conversion, convert_tool, document_source = case(monkeypatch)
    original = _payload(await invoke(conversion, convert_tool))
    assert original["status"] == "success"
    source = PostgresAuditExportSource(db)
    # 認可は source の専用回帰で検証し、ここでは実 repository/byte/commit を接続する。
    source._authorize = AsyncMock()
    catalog = create_run_tool_registry(
        ContractStore(CONTRACTS), document_source=document_source,
        artifact_append_provider=ArtifactAppendProvider(source),
    )
    tool = catalog.resolve_unbound("artifact.append/v1", execution_profile="SUPERVISED")
    workspace = WorkspaceManager((tmp_path / "runs").resolve()).initialize(db.run.id)
    context = replace(conversion.gateway._context, tools=(tool,), workspace=workspace,
                      permission_snapshot={"allowed_capabilities": ["artifact.append/v1"],
                                           "execution_profile": "SUPERVISED"})
    runtime = catalog.build_gateway_runtime(context, audit_writer=db.writer)
    text = "\n\n## RV 確認済みの適用範囲\n除外なし。\n"
    request = {"artifact_ref": original["artifact_refs"][0], "text": text, "purpose": "RV scope"}
    session_id = "00000000-0000-4000-8000-000000000aaa"
    await runtime.mcp.on_tool_authorized(tool.sdk_name, request, "append-original", session_id)
    result = _payload(await runtime.gateway.invoke_mcp(tool.sdk_name, request))
    assert result["status"] == "success", result
    assert db.timeline[-1] == "commit"
    reference, = result["artifact_refs"]
    assert reference != request["artifact_ref"]
    final = await ArtifactRepository(db()).get_content(
        project_id=db.run.project_id, run_id=db.run.id, artifact_ref=reference,
    )
    assert final.content == original["markdown"].encode() + text.encode()
    assert (workspace.root / final.metadata.path).read_bytes() == final.content
    assert (await _result(db, reference)).artifact_refs == frozenset({reference})
    await runtime.mcp.on_tool_authorized(tool.sdk_name, request, "append-original", session_id)
    assert _payload(await runtime.gateway.invoke_mcp(tool.sdk_name, request)) == result
    assert len(db.rows(Evidence)) == 3 and len(db.rows(ToolCall)) == 2
    final_row = next(row for row in db.rows(Evidence) if row.artifact_ref == reference)
    final_row.artifact_bytes = b"tampered"
    await runtime.mcp.on_tool_authorized(tool.sdk_name, request, "append-original", session_id)
    reply = await runtime.gateway.invoke_mcp(tool.sdk_name, request)
    assert reply.get("is_error"), reply
    assert len(db.rows(Evidence)) == 3


@pytest.mark.parametrize("large", [False, True])
async def test_file_delivery_keeps_exact_bytes_without_full_tool_body(monkeypatch, tmp_path, large):
    """実変換から監査/原 byte 読取まで通し、旧 inline と同じ成果を小応答で渡す。"""
    from uuid import uuid4

    from skillmind.agent.workspace import WorkspaceManager

    workspace = WorkspaceManager(tmp_path / "runs").initialize(uuid4())
    db, runtime, tool, source = case(monkeypatch, workspace=workspace)
    if large:
        from unittest.mock import AsyncMock

        from skillmind.agent.binary_text import MarkdownConversion

        monkeypatch.setattr(
            "skillmind.agent.document_provider.convert_excel_to_markdown",
            AsyncMock(return_value=MarkdownConversion("日本語😀" * 20000 + "\n")),
        )
    reply = await invoke(runtime, tool, response_mode="file")
    assert not reply.get("is_error"), reply
    response = _payload(reply)
    assert "markdown" not in response
    assert len(json.dumps(response, ensure_ascii=False)) < 4000
    local = workspace.root / response["file"]["path"]
    repository = ArtifactRepository(db())
    saved = await repository.get_content(
        project_id=db.claimed.project_id, run_id=db.claimed.run_id,
        artifact_ref=response["artifact_refs"][0],
    )
    assert saved is not None and saved.content == local.read_bytes()
    assert response["file"]["content_hash"] == saved.metadata.checksum
    assert len(await repository.list_metadata(
        project_id=db.claimed.project_id, run_id=db.claimed.run_id,
    )) == 1
    assert _payload(await invoke(runtime, tool, response_mode="file")) == response
    assert len(source.calls) == 1
    await _result(db, saved.metadata.artifact_ref)
    # 保存 metadata の改変を本文取得・一覧の両方で拒否する。
    call = db.rows(ToolCall)[0]
    call.result_json["file"]["content_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ArtifactIntegrityError):
        await repository.list_metadata(project_id=db.claimed.project_id, run_id=db.claimed.run_id)
