"""Project 文書 upload/列挙/download/削除 API の契約と作用域を検証する。"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from skillmind.documents.domain import DocumentStorageUnavailableError
from skillmind.storage import FileStorageError
from tests.api.fakes import FakeDocumentService


def test_upload_document_returns_created_metadata(client: TestClient) -> None:
    """Multipart upload が folder 付きで文書を保存し、metadata を返す。"""

    fake = FakeDocumentService()
    client.app.state.document_service = fake
    project_id = uuid4()
    response = client.post(
        f"/api/v1/projects/{project_id}/documents",
        headers={"Idempotency-Key": str(uuid4())},
        data={"folder": "specs"},
        files={"file": ("overview.md", b"# Overview\n", "text/markdown")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["project_id"] == str(project_id)
    assert body["name"] == "overview.md"
    assert body["folder"] == "specs"
    assert "storage_key" not in body  # 内部 key は公開しない。
    assert set(body) == {
        "document_id", "project_id", "folder", "name", "size", "mime", "checksum",
        "uploaded_by", "created_at",
    }
    assert fake.uploaded == [("specs", "overview.md", b"# Overview\n")]


def test_list_documents_returns_project_scoped_records(client: TestClient) -> None:
    """一覧は Project 作用域の文書 metadata を返す。"""

    client.app.state.document_service = FakeDocumentService()
    project_id = uuid4()
    response = client.get(f"/api/v1/projects/{project_id}/documents")

    assert response.status_code == 200
    documents = response.json()["documents"]
    assert len(documents) == 1
    assert documents[0]["project_id"] == str(project_id)


def test_download_document_streams_content_with_mime(client: TestClient) -> None:
    """Download は blob 正文を content-type と共に返す。"""

    client.app.state.document_service = FakeDocumentService()
    response = client.get(
        f"/api/v1/projects/{uuid4()}/documents/{uuid4()}/content"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.content == b"document-body"
    # text/html を allowlist 化できる安全前提: content endpoint は常に attachment を強制し、
    # 同源で inline 描画される経路を残さない。
    assert response.headers["content-disposition"].startswith("attachment;")


def test_download_missing_document_folds_to_not_found(client: TestClient) -> None:
    """他 Project/不存在の文書 download は 404 の安定 Problem へ畳む。"""

    client.app.state.document_service = FakeDocumentService(not_found=True)
    response = client.get(
        f"/api/v1/projects/{uuid4()}/documents/{uuid4()}/content"
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "document_not_found"


def test_delete_document_returns_no_content(client: TestClient) -> None:
    """削除は 204 を返し、service に削除を記録する。"""

    fake = FakeDocumentService()
    client.app.state.document_service = fake
    document_id = uuid4()
    response = client.delete(f"/api/v1/projects/{uuid4()}/documents/{document_id}")

    assert response.status_code == 204
    assert fake.deleted == [document_id]


def test_upload_over_quota_returns_unprocessable(client: TestClient) -> None:
    """配額超過の upload は 422 の安定 code を返す。"""

    client.app.state.document_service = FakeDocumentService(quota_exceeded=True)
    response = client.post(
        f"/api/v1/projects/{uuid4()}/documents",
        headers={"Idempotency-Key": str(uuid4())},
        files={"file": ("big.txt", b"x" * 32, "text/plain")},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "project_quota_exceeded"


def test_upload_duplicate_name_returns_conflict(client: TestClient) -> None:
    """同一 folder/name の重複 upload は 409 の安定 code を返す。"""

    client.app.state.document_service = FakeDocumentService(conflict=True)
    response = client.post(
        f"/api/v1/projects/{uuid4()}/documents",
        headers={"Idempotency-Key": str(uuid4())},
        files={"file": ("overview.md", b"# Overview\n", "text/markdown")},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "document_conflict"


@pytest.mark.parametrize("error_type", [DocumentStorageUnavailableError, FileStorageError])
def test_upload_storage_refusal_or_unknown_uses_static_no_store_problem(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
) -> None:
    """未設定/別保存先と PUT 不明を同じ 503 に閉じ、原 key や SDK 本文を返さない。"""

    fake = FakeDocumentService()
    operation = AsyncMock(side_effect=error_type("private namespace key and endpoint"))
    monkeypatch.setattr(fake, "upload_document", operation)
    client.app.state.document_service = fake
    response = client.post(
        f"/api/v1/projects/{uuid4()}/documents",
        headers={"Idempotency-Key": str(uuid4())},
        files={"file": ("note.txt", b"note", "text/plain")},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "document_storage_unavailable"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "private" not in response.text and "endpoint" not in response.text
    operation.assert_awaited_once()
