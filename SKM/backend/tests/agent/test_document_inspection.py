"""公開 metadata Tool の権限・原選択・本文未取得・Evidence・停止を検証する。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from skillmind.agent.context_builder import ContractStore, document_inspect_tool_definition
from skillmind.agent.document_inspection import DocumentInspectProvider
from skillmind.agent.tool_gateway import ToolProviderError, ToolRegistry
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.documents.source import ProjectDocumentObservation
from skillmind.storage import FileStorageError
from skillmind.storage.observation import BlobObservation
from tests.agent.test_document_conversion import _conversion_context
from tests.agent.test_document_provider import _FakeSource
from tests.agent.test_tool_gateway import MemoryAuditWriter
from tests.documents.fakes import document_content, document_snapshot

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _case(monkeypatch: pytest.MonkeyPatch, *, version: str | None = "original-version"):
    """本文 port は呼ばれれば失敗し、metadata だけを明示的に返す source を構築する。"""

    content = document_content(b"not read", name="cases.xlsx")
    context = _conversion_context(content)
    assert context.run is not None
    tool = replace(context.tool, capability="document.inspect/v1")
    sources = {
        key: {**value, "capability": "document.inspect/v1", "preparation_policy": "on-demand/v1"}
        for key, value in context.run.resolved_sources.items()
    }
    run = replace(
        context.run,
        tools=(tool,),
        resolved_sources=sources,
        permission_snapshot={"allowed_capabilities": ["document.inspect/v1"]},
    )
    context = replace(context, run=run, tool=tool)
    observed = ProjectDocumentObservation(
        context.project_id,
        document_snapshot(context.project_id, [content]).documents[0],
        BlobObservation(
            last_modified=datetime(2026, 9, 11, 0, 1, tzinfo=UTC),
            etag="opaque-etag",
            version_id=version,
            size=content.size,
            content_type=content.mime,
        ),
        "sha256:" + "b" * 64,
    )
    source = _FakeSource(project_id=context.project_id, content=content)
    inspect = AsyncMock(return_value=observed)
    monkeypatch.setattr(source, "inspect", inspect, raising=False)
    monkeypatch.setattr(
        source, "fetch_observed", AsyncMock(side_effect=AssertionError("No GET")), raising=False
    )
    monkeypatch.setattr(source, "fetch", AsyncMock(side_effect=AssertionError("No GET")))
    return context, source, inspect, observed


@pytest.mark.parametrize("version", [None, "null", "original-version"])
@pytest.mark.parametrize("source_key", [None, "projects/original/documents/frozen-source.xlsx"])
async def test_metadata_response_matches_contract_without_claiming_content_verification(
    monkeypatch: pytest.MonkeyPatch,
    version: str | None,
    source_key: str | None,
) -> None:
    """LastModified は source の実観測であり、hash は観測 JSON と元本文を区別する。"""

    context, source, inspect, observed = _case(monkeypatch, version=version)
    observed = replace(observed, source_object_key=source_key)
    inspect.return_value = observed
    result = await DocumentInspectProvider(source).execute(context, {"path": "specs/cases.xlsx"})
    response = {**result.response, "evidence_refs": ["ev_metadata_001"]}
    schema = ContractStore(CONTRACTS).load("tools/document.inspect/v1/response.schema.json")
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(response)
    assert response["content_verified"] is False
    assert response["storage"]["last_modified"] == "2026-09-11T00:01:00+00:00"
    assert response["storage"]["version_id"] == version
    assert response["document"]["content_hash"] == observed.document.content_hash
    observation = result.evidence[0].metadata["observation"]
    assert response["observation_checksum"] == "sha256:" + sha256_hex(canonical_json(observation))
    assert result.evidence[0].content_hash == response["observation_checksum"]
    assert result.evidence[0].content_hash != observed.document.content_hash
    assert result.evidence[0].metadata["source_reference_checksum"] == observed.reference_checksum
    assert "source_reference_checksum" not in response
    assert "bucket" not in canonical_json(response)
    if source_key is None:
        assert "source_object_key" not in response
        assert result.evidence[0].metadata["observation_version"] == "v1"
    else:
        assert response["source_object_key"] == source_key != observed.document.path
        assert observation["source_object_key"] == source_key
        assert result.evidence[0].metadata["observation_version"] == "v2"
    inspect.assert_awaited_once_with(
        project_id=context.project_id, document_id=observed.document.document_id
    )
    source.fetch.assert_not_called()
    source.fetch_observed.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "specs/not-selected.xlsx",
        "../cases.xlsx",
        "/specs/cases.xlsx",
        "specs//cases.xlsx",
        "specs/./cases.xlsx",
        "specs\\cases.xlsx",
        "specs/cases.xlsx/",
    ],
)
async def test_nonexact_or_unselected_path_never_inspects_storage(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """完全一致しない path は正規化で救済せず、存在確認も行わない。"""

    context, source, inspect, _ = _case(monkeypatch)
    with pytest.raises(ToolProviderError):
        await DocumentInspectProvider(source).execute(context, {"path": path})
    inspect.assert_not_called()


@pytest.mark.parametrize("case", ["permission", "tool", "project", "run", "attempt", "actor"])
async def test_inspection_requires_explicit_permission_and_original_run_identity(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """既存 read/convert 権や偽造 context では metadata 取得も始めない。"""

    context, source, inspect, _ = _case(monkeypatch)
    if case == "permission":
        context = replace(
            context,
            run=replace(
                context.run, permission_snapshot={"allowed_capabilities": ["document.convert/v1"]}
            ),
        )
    elif case == "tool":
        context = replace(context, tool=replace(context.tool, capability="document.read/v1"))
    else:
        field = {
            "project": "project_id",
            "run": "run_id",
            "attempt": "run_attempt_id",
            "actor": "user_id",
        }[case]
        context = replace(context, **{field: uuid4()})
    with pytest.raises(ToolProviderError) as caught:
        await DocumentInspectProvider(source).execute(context, {"path": "specs/cases.xlsx"})
    assert caught.value.code == "scope_denied"
    inspect.assert_not_called()


@pytest.mark.parametrize("case", ["missing", "document", "project", "size", "unavailable"])
async def test_untrusted_or_missing_metadata_is_not_a_successful_observation(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """原文書との不一致や存否不明を空 metadata・存在なし・成功に丸めない。"""

    context, source, inspect, observed = _case(monkeypatch)
    if case == "missing":
        inspect.return_value = None
    elif case == "document":
        inspect.return_value = replace(
            observed, document=replace(observed.document, name="changed.xlsx")
        )
    elif case == "project":
        inspect.return_value = replace(observed, project_id=uuid4())
    elif case == "size":
        inspect.return_value = replace(
            observed, observation=replace(observed.observation, size=999)
        )
    else:
        inspect.side_effect = FileStorageError("private connection and key")
    with pytest.raises(ToolProviderError) as caught:
        await DocumentInspectProvider(source).execute(context, {"path": "specs/cases.xlsx"})
    assert caught.value.code == "unavailable" and "private" not in caught.value.message


async def test_cancellation_propagates_after_owned_coroutine_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消は観測成功に変えず、source coroutine の後片付け後に呼出元へ戻す。"""

    context, source, inspect, _ = _case(monkeypatch)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def wait_for_cancel(**kwargs):
        """遅延 metadata 読取を模倣し、取消の伝播を記録する。"""
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    inspect.side_effect = wait_for_cancel
    task = asyncio.create_task(
        DocumentInspectProvider(source).execute(context, {"path": "specs/cases.xlsx"})
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


@pytest.mark.parametrize("source_key", [None, "original/frozen-source.xlsx"])
async def test_gateway_commits_metadata_evidence_and_replays_the_same_observation(
    monkeypatch: pytest.MonkeyPatch,
    source_key: str | None,
) -> None:
    """実 Gateway 契約と監査 port を通し、同じ成功済み呼出しは再 HEAD しない。"""

    context, source, inspect, observed = _case(monkeypatch)
    inspect.return_value = replace(observed, source_object_key=source_key)
    registry = ToolRegistry((document_inspect_tool_definition(ContractStore(CONTRACTS), source),))
    registered = registry.resolve(
        "document.inspect/v1", provider="project-documents", integration_id=None
    )
    run = replace(context.run, tools=(registered,))
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(run, audit_writer=writer)
    assert runtime.mcp.on_tool_authorized is not None
    arguments = {"path": "specs/cases.xlsx", "purpose": "Observe storage metadata"}
    session_id = str(uuid4())
    results = []
    for _ in range(2):
        await runtime.mcp.on_tool_authorized(
            registered.sdk_name, arguments, "inspect-once", session_id
        )
        result = await runtime.gateway.invoke_mcp(registered.sdk_name, arguments)
        assert not result.get("is_error")
        results.append(json.loads(result["content"][0]["text"]))
    assert results[0] == results[1]
    assert results[0]["evidence_refs"][0].startswith("ev_")
    assert len(writer.completed) == 1 and inspect.await_count == 1
    saved = writer.completed[0][1][0]
    assert saved.draft.metadata["observation"]["content_verified"] is False
    assert saved.draft.content_hash == results[0]["observation_checksum"]
