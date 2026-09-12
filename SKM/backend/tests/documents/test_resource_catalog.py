"""変換候補の能力を文書形式と全集の実メンバーに照合する。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from skillmind.documents.library import DOCUMENT_LIBRARY_SELECTION, DOCUMENT_WRITE_CAPABILITY
from skillmind.documents.repository import DocumentRepository
from skillmind.documents.resource_catalog import DocumentResourceCatalog
from tests.documents.fakes import document_content, stored_document
from tests.documents.test_document_library_binding import target


@pytest.mark.parametrize("excel", [False, True])
async def test_conversion_is_offered_only_for_excel_candidates(
    monkeypatch: pytest.MonkeyPatch, excel: bool,
) -> None:
    """非 Excel 単体に変換能力を表示せず、全集には対象が存在する時だけ表示する。"""

    project = uuid4()
    names = ["notes.md", "macro.xlsm"] + (["book.XLSX", "legacy.xls"] if excel else [])
    documents = [stored_document(project, document_content(name=name)) for name in names]
    listing = AsyncMock(return_value=documents)
    monkeypatch.setattr(DocumentRepository, "list_for_project", listing)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    candidates = await DocumentResourceCatalog(factory).candidates(project_id=project)
    listing.assert_awaited_once_with(project)
    for candidate in candidates[:-1]:
        assert "document.inspect/v1" in candidate.capabilities
        assert "document.list/v1" in candidate.capabilities
        assert ("document.convert/v1" in candidate.capabilities) == (
            candidate.label.lower().endswith((".xls", ".xlsx"))
        )
    assert ("document.convert/v1" in candidates[-1].capabilities) == excel


@pytest.mark.parametrize("configured", [False, True])
async def test_library_candidate_is_available_even_before_any_input_upload(monkeypatch, configured):
    """空の文書庫も保存先に選べるが、未所属 storage の候補や内部接続情報は提示しない。"""

    monkeypatch.setattr(DocumentRepository, "list_for_project", AsyncMock(return_value=[]))
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    candidates = await DocumentResourceCatalog(
        factory, library_target=target() if configured else None
    ).candidates(project_id=uuid4())
    if not configured:
        assert candidates == ()
        return
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.key == DOCUMENT_LIBRARY_SELECTION
    assert candidate.capabilities == (DOCUMENT_WRITE_CAPABILITY,)
    assert candidate.scope is None and candidate.integration_id is None
