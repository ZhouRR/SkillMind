"""文書削除の原 credential、静的 Problem と精確 metadata 読取を検証する。"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fakes import FakeDocumentService
from fastapi import FastAPI
from fastapi.testclient import TestClient

from projectmind.auth.domain import generate_session_credentials
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.documents.domain import (
    DocumentInUseError,
    DocumentNotFoundError,
    DocumentReferencesUnavailableError,
    DocumentStorageUnavailableError,
    DocumentUploadInvalidError,
)
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from projectmind.storage import FileStorageError
from projectmind.users.domain import UserAccess


@pytest.mark.parametrize(
    "error,status,code",
    [
        (DocumentInUseError, 409, "document_in_use"),
        (DocumentReferencesUnavailableError, 409, "document_references_unavailable"),
        (DocumentNotFoundError, 404, "document_not_found"),
        (UnauthorizedSessionError, 401, "authentication_required"),
        (CsrfRejectedError, 403, "csrf_rejected"),
        (ProjectNotFoundError, 404, "project_not_found"),
        (ProjectArchivedError, 409, "project_archived"),
        (DocumentStorageUnavailableError, 503, "document_storage_unavailable"),
        (DocumentUploadInvalidError, 503, "document_upload_unavailable"),
        (FileStorageError, 503, "document_storage_unavailable"),
    ],
)
def test_delete_maps_transaction_refusals_and_storage_unknown_without_details(
    client: TestClient,
    error: type[Exception],
    status: int,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じ route を通り、内部参照や storage の例外本文を公開しない。"""

    fake = FakeDocumentService()
    assert isinstance(client.app, FastAPI)
    spy = AsyncMock(side_effect=error("private reference data"))
    monkeypatch.setattr(fake, "delete_document", spy)
    client.app.state.document_service = fake
    response = client.delete(f"/api/v1/projects/{uuid4()}/documents/{uuid4()}")
    assert response.status_code == status
    assert response.json()["code"] == code
    assert "private reference data" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/problem+json")
    spy.assert_awaited_once()


def test_delete_passes_original_cookie_csrf_and_server_request_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入口 Actor だけに縮めず、元の会話と CSRF を業務 transaction へ渡す。"""

    fake = FakeDocumentService()
    spy = AsyncMock(wraps=fake.delete_document)
    assert isinstance(client.app, FastAPI)
    monkeypatch.setattr(fake, "delete_document", spy)
    client.app.state.document_service = fake
    credentials = generate_session_credentials()
    auth = client.app.state.auth_service
    auth.session_token, auth.csrf_token = credentials.session_token, credentials.csrf_token
    client.cookies.set(
        client.app.state.settings.auth_session_cookie_name, credentials.session_token
    )
    response = client.delete(
        f"/api/v1/projects/{uuid4()}/documents/{uuid4()}",
        headers={"X-CSRF-Token": credentials.csrf_token},
    )
    assert response.status_code == 204 and response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert spy.await_args is not None
    access = spy.await_args.kwargs["access"]
    assert isinstance(access, UserAccess) and isinstance(access.request_id, UUID)
    assert access.session_token == credentials.session_token
    assert access.csrf_token == credentials.csrf_token
    assert str(access.request_id) == response.headers["x-request-id"]


@pytest.mark.parametrize("missing", [False, True])
def test_exact_metadata_lookup_has_no_storage_or_mutation_effect(
    client: TestClient, missing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原 ID の metadata/安定 404 だけを返し、download を確認手段にしない。"""

    fake = FakeDocumentService(not_found=missing)
    assert isinstance(client.app, FastAPI)
    download = AsyncMock(side_effect=AssertionError("No content read"))
    deletion = AsyncMock(side_effect=AssertionError("No deletion"))
    monkeypatch.setattr(fake, "download_document", download)
    monkeypatch.setattr(fake, "delete_document", deletion)
    client.app.state.document_service = fake
    project_id, document_id = uuid4(), uuid4()
    response = client.get(f"/api/v1/projects/{project_id}/documents/{document_id}")
    assert response.status_code == (404 if missing else 200)
    assert response.headers["cache-control"] == "no-store"
    if missing:
        assert response.json()["code"] == "document_not_found"
    else:
        assert response.json()["document_id"] == str(document_id)
        assert response.json()["project_id"] == str(project_id)
        assert set(response.json()) == {
            "document_id",
            "project_id",
            "folder",
            "name",
            "size",
            "mime",
            "checksum",
            "uploaded_by",
            "created_at",
        }
    download.assert_not_called()
    deletion.assert_not_called()
