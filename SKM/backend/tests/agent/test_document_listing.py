"""実 Provider/Gateway を通して分頁範囲・観測 Evidence・失敗停止を検証する。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.agent.context_builder import ContractStore, document_list_tool_definition
from skillmind.agent.document_listing import DocumentListProvider
from skillmind.agent.tool_gateway import ToolProviderError, ToolRegistry
from skillmind.documents.source import ProjectDocumentObservation
from skillmind.storage import FileStorageError
from skillmind.storage.observation import BlobObservation
from tests.agent.test_document_provider import CONTRACTS, _context
from tests.agent.test_tool_gateway import MemoryAuditWriter
from tests.documents.fakes import document_content, document_snapshot


def _case(count=3):
    """先頭は前日、後続は当日の metadata とし、本文 port は呼出しを拒否する。"""
    project_id = uuid4()
    contents = [document_content(b"abc", name=f"{i}.xlsx") for i in range(count)]
    snapshot = document_snapshot(project_id, contents)
    context = _context(project_id, content=contents[0])
    tool = replace(context.tool, capability="document.list/v1")
    run = replace(
        context.run,
        tools=(tool,),
        permission_snapshot={"allowed_capabilities": ["document.list/v1"]},
        resolved_sources={
            "config": {
                "capability": "document.list/v1",
                "provider": "project-documents",
                "preparation_policy": "on-demand/v1",
                "document_snapshot": snapshot.to_json(),
            }
        },
    )
    context = replace(context, run=run, tool=tool)
    observations = {
        d.document_id: ProjectDocumentObservation(
            project_id,
            d,
            BlobObservation(
                datetime(2026, 9, 10 if d.name == "0.xlsx" else 11, tzinfo=UTC),
                "opaque",
                "original",
                d.size,
                d.mime,
            ),
            "sha256:" + "b" * 64,
        )
        for d in snapshot.documents
    }

    async def inspect(*, project_id, document_id):
        """原 Project と具体 ID だけで HEAD 結果を返す storage port。"""
        assert project_id == context.project_id
        return observations[document_id]

    source = SimpleNamespace(
        inspect=AsyncMock(side_effect=inspect),
        fetch=AsyncMock(side_effect=AssertionError("No GET")),
        fetch_observed=AsyncMock(side_effect=AssertionError("No GET")),
    )
    return context, source, observations


async def test_gateway_pages_continue_after_no_matches_and_replay_saved_observations():
    """実公開契約・監査 port の順序を通し、成功済み page の再呼出しは HEAD しない。"""
    context, source, _ = _case()
    registry = ToolRegistry((document_list_tool_definition(ContractStore(CONTRACTS), source),))
    registered = registry.resolve(
        "document.list/v1", provider="project-documents", integration_id=None
    )
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(
        replace(context.run, tools=(registered,)), audit_writer=writer
    )
    session_id = str(uuid4())
    arguments = {
        "directory": "specs/",
        "limit": 1,
        "purpose": "Observe selected specifications",
        "modified_on": {
            "date": "2026-09-11",
            "timezone": "Asia/Tokyo",
            "not_after": "2026-09-11T12:00:00+09:00",
        },
    }
    pages = []
    for page in range(3):
        responses = []
        for _ in range(2):
            await runtime.mcp.on_tool_authorized(
                registered.sdk_name, arguments, f"page-{page}", session_id
            )
            result = await runtime.gateway.invoke_mcp(registered.sdk_name, arguments)
            assert not result.get("is_error"), result
            responses.append(json.loads(result["content"][0]["text"]))
        assert responses[0] == responses[1]
        response = responses[0]
        pages.append(response)
        assert response["scope"] == "run_frozen_documents"
        assert response["candidate_count"] == response["frozen_count"] == 3
        assert (response["scan_start"], response["scan_end"]) == (page, page + 1)
        entry = response["entries"][0]
        assert entry["document"]["name"] == f"{page}.xlsx"
        assert entry["matches_filter"] is (page > 0)
        assert entry["content_verified"] is False
        assert entry["evidence_index"] == 1
        assert len(response["evidence_refs"]) == 2
        saved = writer.completed[page][1]
        assert saved[0].draft.evidence_type == "document_listing"
        assert saved[1].draft.content_hash == entry["observation_checksum"]
        assert saved[1].evidence_ref == response["evidence_refs"][entry["evidence_index"]]
        arguments = {**arguments, "cursor": response["next_cursor"]}
    assert pages[0]["next_cursor"] is not None
    assert pages[-1]["next_cursor"] is None
    assert source.inspect.await_count == 3
    source.fetch.assert_not_called()
    source.fetch_observed.assert_not_called()


async def test_empty_directory_still_has_explicit_coverage_evidence():
    """候補なし page は成功監査を持ち、未選択の bucket メンバーを列挙しない。"""
    context, source, _ = _case()
    result = await DocumentListProvider(source).execute(context, {"directory": "other"})
    assert result.response["entries"] == []
    assert result.response["candidate_count"] == 0
    assert result.response["next_cursor"] is None
    assert result.response["scan_end"] == result.response["scan_start"] == 0
    assert len(result.evidence) == 1
    source.inspect.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"directory": "other"},
        {"limit": 2},
        {"recursive": False},
        {"exclude_name_prefixes": ["0"]},
        {"extensions": [".xls"]},
    ],
)
async def test_cursor_cannot_continue_a_different_query(change):
    """page の続きは原条件と幅に固定し、条件変更時の中間開始を拒否する。"""
    context, source, _ = _case()
    provider = DocumentListProvider(source)
    result = await provider.execute(context, {"directory": "specs", "limit": 1})
    source.inspect.reset_mock()
    with pytest.raises(ToolProviderError) as error:
        await provider.execute(
            context,
            {"directory": "specs", "limit": 1, "cursor": result.response["next_cursor"], **change},
        )
    assert error.value.code == "invalid_request"
    source.inspect.assert_not_called()


@pytest.mark.parametrize("case", ["run", "selection", "offset", "offset_bool", "null"])
async def test_cursor_is_bound_to_run_and_full_membership(case):
    """別 Run・変更された選択・不正 offset は HEAD の前に拒否する。"""
    context, source, _ = _case()
    provider = DocumentListProvider(source)
    result = await provider.execute(context, {"directory": "specs", "limit": 1})
    cursor = dict(result.response["next_cursor"])
    if case == "run":
        run_id = uuid4()
        context = replace(context, run_id=run_id, run=replace(context.run, run_id=run_id))
    elif case == "selection":
        contents = [document_content(b"abc", name=f"{i}.xlsx") for i in range(4)]
        context.run.resolved_sources["config"]["document_snapshot"] = document_snapshot(
            context.project_id, contents
        ).to_json()
    elif case == "offset":
        cursor["offset"] = 3
    elif case == "offset_bool":
        cursor["offset"] = True
    else:
        cursor = None
    source.inspect.reset_mock()
    with pytest.raises(ToolProviderError):
        await provider.execute(context, {"directory": "specs", "limit": 1, "cursor": cursor})
    source.inspect.assert_not_called()


@pytest.mark.parametrize(
    "case",
    [
        "permission",
        "identity",
        "metadata",
        "missing",
        "error",
        "scope_changed",
        "permission_changed",
    ],
)
async def test_scope_or_metadata_failure_never_returns_partial_page(case):
    """失権・欠落・途中失敗は対象なしや部分成功に変えない。"""
    context, source, observations = _case()
    first = next(o for o in observations.values() if o.document.name == "0.xlsx")
    if case == "permission":
        context.run.permission_snapshot["allowed_capabilities"] = ["document.read/v1"]
    elif case == "identity":
        context = replace(context, user_id=uuid4())
    elif case == "metadata":
        source.inspect.side_effect = [first, replace(first, project_id=uuid4())]
    elif case == "missing":
        source.inspect.side_effect = [first, None]
    elif case == "error":
        source.inspect.side_effect = [first, FileStorageError("HEAD failed")]
    else:

        async def change_scope(**kwargs):
            """await 中の入れ子 scope 変更を再検証で拒否させる。"""
            if case == "permission_changed":
                context.run.permission_snapshot["allowed_capabilities"] = []
            else:
                context.run.resolved_sources.clear()
            return first

        source.inspect.side_effect = change_scope
    with pytest.raises(ToolProviderError):
        await DocumentListProvider(source).execute(context, {"directory": "specs"})
    source.fetch.assert_not_called()
    if case in {"permission", "identity"}:
        source.inspect.assert_not_called()


async def test_cancellation_propagates_after_metadata_coroutine_cleanup():
    """page 取消を成功へ変換せず、source coroutine の終了を待つ。"""
    context, source, _ = _case()
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def wait_for_cancel(**kwargs):
        """遅延 HEAD port の取消完了を記録する。"""
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    source.inspect.side_effect = wait_for_cancel
    task = asyncio.create_task(DocumentListProvider(source).execute(context, {"directory": ""}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


@pytest.mark.parametrize("limit", [0, 51, True, "1", None])
async def test_invalid_page_width_performs_no_storage_io(limit):
    """巨大 page と型の暗黙変換を拒否し、有界 HEAD 数を維持する。"""
    context, source, _ = _case()
    with pytest.raises(ToolProviderError) as error:
        await DocumentListProvider(source).execute(context, {"directory": "", "limit": limit})
    assert error.value.code == "invalid_request"
    source.inspect.assert_not_called()


async def test_default_page_caps_observations_and_preserves_next_cursor():
    """51 候補の先頭 page は 50 HEAD に留まり、残る候補を切り捨てない。"""
    context, source, _ = _case(51)
    result = await DocumentListProvider(source).execute(context, {"directory": ""})
    assert len(result.response["entries"]) == source.inspect.await_count == 50
    assert result.response["next_cursor"]["offset"] == 50
    assert result.response["candidate_count"] == 51


async def test_page_timeout_does_not_publish_partial_observations(monkeypatch):
    """30 秒 page deadline の処理経路を短い時計で検証し、遅い HEAD を終了させる。"""
    context, source, observations = _case()
    original_timeout = asyncio.timeout
    deadlines = []
    cleaned = asyncio.Event()

    def short_timeout(seconds):
        """本番指定期限を記録し、テストだけ待機時間を縮める。"""
        deadlines.append(seconds)
        return original_timeout(0.02)

    async def slow_second(*, project_id, document_id):
        """一份の成功後に遅延し、途中成果が返らないことを確認する。"""
        observed = observations[document_id]
        if observed.document.name == "0.xlsx":
            return observed
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(asyncio, "timeout", short_timeout)
    source.inspect.side_effect = slow_second
    with pytest.raises(ToolProviderError) as error:
        await DocumentListProvider(source).execute(context, {"directory": ""})
    assert error.value.code == "unavailable"
    assert deadlines == [30] and source.inspect.await_count == 2
    assert cleaned.is_set()
