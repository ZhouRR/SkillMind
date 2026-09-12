"""観測引用からの原文取得・実 MarkItDown・失敗時の非降格を Provider 境界で検証する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from openpyxl import Workbook

from skillmind.agent.document_provider import DocumentConvertProvider
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.documents.source import ProjectDocumentObservation
from skillmind.storage.observation import BlobObservation
from tests.agent.test_document_conversion import _conversion_context
from tests.agent.test_document_provider import _FakeSource
from tests.documents.fakes import document_content, document_snapshot


def _case(monkeypatch):
    """実 Excel と元観測を作り、通常 fetch と新 HEAD は失敗する source を渡す。"""
    book = Workbook()
    sheet = book.active
    sheet.append(["Expected"])
    sheet.append(["PASS"])
    stream = BytesIO()
    book.save(stream)
    content = document_content(stream.getvalue(), name="cases.xlsx")
    context = _conversion_context(content)
    storage = BlobObservation(
        datetime(2026, 9, 11, tzinfo=UTC), "opaque", "original-version", content.size, content.mime
    )
    observed = ProjectDocumentObservation(
        context.project_id,
        document_snapshot(context.project_id, [content]).documents[0],
        storage,
        "sha256:" + "b" * 64,
    )
    lookup = SimpleNamespace(load=AsyncMock(return_value=observed))
    source = _FakeSource(project_id=context.project_id, content=content)
    monkeypatch.setattr(source, "fetch", AsyncMock(side_effect=AssertionError("No ordinary fetch")))
    monkeypatch.setattr(
        source, "inspect", AsyncMock(side_effect=AssertionError("No new inspection")), raising=False
    )
    acquire = AsyncMock(return_value=replace(content, observation=storage))
    monkeypatch.setattr(source, "fetch_observed", acquire, raising=False)
    return SimpleNamespace(
        content=content,
        context=context,
        source=source,
        lookup=lookup,
        observed=observed,
        acquire=acquire,
        provider=DocumentConvertProvider(source, observations=lookup),
    )


async def test_saved_observation_pins_real_conversion_and_is_linked_in_evidence(monkeypatch):
    """元観測と同じ bytes を native converter へ渡し、変換 Evidence に元参照を記録する。"""
    case = _case(monkeypatch)
    result = await case.provider.execute(
        case.context, {"path": "specs/cases.xlsx", "observation_ref": "ev_original"}
    )
    case.lookup.load.assert_awaited_once_with(
        project_id=case.context.project_id,
        run_id=case.context.run_id,
        document=case.observed.document,
        reference="ev_original",
    )
    case.acquire.assert_awaited_once_with(
        project_id=case.context.project_id, observed=case.observed
    )
    assert result.response["markdown"]
    assert result.response["converter"] == {"name": "markitdown", "version": "0.1.7"}
    assert result.response["document"]["checksum"] == case.content.checksum
    assert result.evidence[0].metadata["observation_ref"] == "ev_original"
    assert result.evidence[0].metadata["storage_observation"] == case.observed.observation.to_json()
    case.source.fetch.assert_not_called()
    case.source.inspect.assert_not_called()


@pytest.mark.parametrize("reference", [None, "", "not a ref", "ev_" + "a" * 65])
async def test_malformed_reference_never_queries_or_downloads(monkeypatch, reference):
    """明示された不正参照を引数なしの旧取得へ読み替えない。"""
    case = _case(monkeypatch)
    with pytest.raises(ToolProviderError) as caught:
        await case.provider.execute(
            case.context, {"path": "specs/cases.xlsx", "observation_ref": reference}
        )
    assert caught.value.code == "invalid_request"
    case.lookup.load.assert_not_called()
    case.acquire.assert_not_called()
    case.source.fetch.assert_not_called()


@pytest.mark.parametrize("case_name", ["missing", "project", "document", "lookup_unavailable"])
async def test_unavailable_or_foreign_observation_fails_before_download(monkeypatch, case_name):
    """来歴不成立では source を呼ばず、別版・別文書へ再解決しない。"""
    case = _case(monkeypatch)
    if case_name == "missing":
        case.lookup.load.return_value = None
    elif case_name == "project":
        case.lookup.load.return_value = replace(case.observed, project_id=uuid4())
    elif case_name == "document":
        case.lookup.load.return_value = replace(
            case.observed, document=replace(case.observed.document, name="other.xlsx")
        )
    else:
        case.provider = DocumentConvertProvider(case.source)
    with pytest.raises(ToolProviderError) as caught:
        await case.provider.execute(
            case.context, {"path": "specs/cases.xlsx", "observation_ref": "ev_original"}
        )
    assert caught.value.code == "unavailable"
    case.acquire.assert_not_called()
    case.source.fetch.assert_not_called()


@pytest.mark.parametrize("case_name", ["missing", "changed_observation", "changed_bytes"])
async def test_acquisition_must_match_both_observation_and_original_bytes(monkeypatch, case_name):
    """取得後も同じ観測・原 hash を確認し、変更は変換前に拒否する。"""
    case = _case(monkeypatch)
    if case_name == "missing":
        case.acquire.return_value = None
    elif case_name == "changed_observation":
        case.acquire.return_value = replace(
            case.content, observation=replace(case.observed.observation, etag="changed")
        )
    else:
        case.acquire.return_value = replace(
            case.content, data=b"changed", observation=case.observed.observation
        )
    with pytest.raises(ToolProviderError) as caught:
        await case.provider.execute(
            case.context, {"path": "specs/cases.xlsx", "observation_ref": "ev_original"}
        )
    assert caught.value.code == "unavailable"
    case.source.fetch.assert_not_called()


async def test_cancel_during_pinned_fetch_waits_for_coroutine_cleanup(monkeypatch):
    """取消は失敗回復による再取得に変換せず、所有 source coroutine へ伝播する。"""
    case = _case(monkeypatch)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def acquire(**kwargs):
        """遅い取得を模倣し、取消後の後片付けを記録する。"""
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    case.acquire.side_effect = acquire
    task = asyncio.create_task(
        case.provider.execute(
            case.context, {"path": "specs/cases.xlsx", "observation_ref": "ev_original"}
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    case.source.fetch.assert_not_called()
