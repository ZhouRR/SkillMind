"""文書 download の静的 Problem と Unicode/攻撃的 metadata の安全な添付を検証する。"""

from __future__ import annotations

import re
from dataclasses import replace
from unittest.mock import AsyncMock
from urllib.parse import unquote
from uuid import uuid4

import pytest
from fakes import FakeDocumentService
from fastapi import FastAPI
from fastapi.testclient import TestClient

from projectmind.documents.domain import (
    DocumentContentInvalidError,
    DocumentContentMissingError,
    DocumentNotFoundError,
    DocumentStorageUnavailableError,
)
from tests.documents.fakes import document_content, stored_document


@pytest.mark.parametrize(
    "error,status,code",
    [
        (DocumentNotFoundError, 404, "document_not_found"),
        (DocumentContentMissingError, 409, "document_content_missing"),
        (DocumentContentInvalidError, 409, "document_content_invalid"),
        (DocumentStorageUnavailableError, 503, "document_storage_unavailable"),
    ],
)
def test_content_errors_are_distinct_static_no_store_problems(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
    status: int,
    code: str,
) -> None:
    """blob 欠損・破損・拒否を metadata 404 と区別し、元 key/SDK 本文を漏らさない。"""

    fake = FakeDocumentService()
    spy = AsyncMock(side_effect=error("private storage key and SDK detail"))
    monkeypatch.setattr(fake, "download_document", spy)
    assert isinstance(client.app, FastAPI)
    client.app.state.document_service = fake
    project_id, document_id = uuid4(), uuid4()
    response = client.get(f"/api/v1/projects/{project_id}/documents/{document_id}/content")
    assert response.status_code == status and response.json()["code"] == code
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text and "SDK" not in response.text
    assert "content-disposition" not in response.headers
    spy.assert_awaited_once_with(project_id=project_id, document_id=document_id)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("overview.md", "overview.md"),
        ("设计文档.md", "设计文档.md"),
        ('a"; injected="value.html', 'a"; injected="value.html'),
        ("bad\r\nX-Test: attack.txt", "bad__X-Test: attack.txt"),
        ("../../outside.txt", "outside.txt"),
        ("folder\\nested\\report.txt", "report.txt"),
        ("/..", "document"),
        ("", "document"),
        ("bad\x00\x7f\u202e.txt", "bad___.txt"),
        ("'百分比%20.xlsx", "'百分比%20.xlsx"),
    ],
)
def test_attachment_filename_uses_utf8_name_and_safe_ascii_fallback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, name: str, expected: str
) -> None:
    """保存名を改変せず、単一 filename として安全に header へ符号化する。"""

    project_id = uuid4()
    content = document_content("正文".encode())
    document = replace(stored_document(project_id, content), name=name, mime="text/html")
    fake = FakeDocumentService()
    monkeypatch.setattr(fake, "download_document", AsyncMock(return_value=(document, content.data)))
    assert isinstance(client.app, FastAPI)
    client.app.state.document_service = fake
    response = client.get(f"/api/v1/projects/{project_id}/documents/{document.document_id}/content")
    assert response.status_code == 200 and response.content == content.data
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.isascii() and "\r" not in disposition and "\n" not in disposition
    match = re.fullmatch(
        r"""attachment; filename="([a-zA-Z0-9 ._-]+)"; filename\*=UTF-8''([^ ]+)""", disposition
    )
    assert match is not None and unquote(match.group(2), errors="strict") == expected
    assert "/" not in match.group(1) and "\\" not in match.group(1)
    assert "x-test" not in response.headers
    assert document.name == name


@pytest.mark.parametrize(
    "mime", ["text/html\r\nX-Test: attack", "text/plain; bogus=value", "中文", "", "a" * 129]
)
def test_unsafe_mime_is_served_only_as_binary_attachment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mime: str
) -> None:
    """壊れた metadata を応答 header に直結せず、正文や保存 MIME は書き換えない。"""

    project_id = uuid4()
    content = document_content()
    document = replace(stored_document(project_id, content), mime=mime)
    fake = FakeDocumentService()
    monkeypatch.setattr(fake, "download_document", AsyncMock(return_value=(document, content.data)))
    assert isinstance(client.app, FastAPI)
    client.app.state.document_service = fake
    response = client.get(f"/api/v1/projects/{project_id}/documents/{document.document_id}/content")
    assert response.status_code == 200 and response.content == content.data
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "x-test" not in response.headers and document.mime == mime
