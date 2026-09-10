"""独立閉鎖 API の原資格・回执・未知と既存 upload 互換性を検証する。"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fakes import DeniedProjectAuthorizationService, FakeDocumentService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.documents.domain import (
    DocumentUploadAlreadyPublishedError,
    DocumentUploadClosureNotFoundError,
    DocumentUploadInvalidError,
    DocumentUploadNotFoundError,
    StoredDocumentUpload,
    StoredDocumentUploadClosure,
)
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError, ProjectStatus
from projectmind.storage import FileStorageError
from projectmind.users.domain import UserAccess
from tests.documents.upload_harness import UploadDatabase

BODY = {"confirmation": "STOP_PUBLICATION"}
FIELDS = {"upload_key", "project_id", "document_id", "closed_at", "publication_state"}


def _app(client: TestClient) -> FastAPI:
    """認証 fixture の実 app を取り、外部 service を起動せず局部 fake を接続する。"""

    assert isinstance(client.app, FastAPI)
    return client.app


def _pending(client: TestClient) -> tuple[FakeDocumentService, UUID, UUID, UUID]:
    """PENDING の非公開原 document ID を fixture に明示し、今日の一覧から推測しない。"""

    app = _app(client)
    fake = FakeDocumentService()
    app.state.document_service = fake
    client.cookies.set(
        app.state.settings.auth_session_cookie_name, app.state.auth_service.session_token
    )
    project, key, document = uuid4(), uuid4(), uuid4()
    identity = (project, app.state.auth_service.actor.user_id, key)
    fake.uploads[identity] = StoredDocumentUpload(
        key,
        project,
        "PENDING",
        datetime(2026, 9, 10, tzinfo=UTC),
        None,
    )
    fake.upload_targets[identity] = document
    return fake, project, key, document


def _url(project: UUID, key: UUID | str) -> str:
    """通常の upload 確認とは独立した閉鎖 resource を指定する。"""

    return f"/api/v1/projects/{project}/document-uploads/{key}/closure"


def test_closure_replays_independent_receipt_and_preserves_original_upload(
    client: TestClient,
) -> None:
    """初回 201/重放 200/読取 200 が同一五項目で、元五項目は PENDING/null のままとする。"""

    fake, project, key, document = _pending(client)
    first = client.post(_url(project, key), json=BODY)
    second = client.post(_url(project, key), json=BODY)
    client.headers.pop("X-CSRF-Token")
    query = client.get(_url(project, key))
    assert (first.status_code, second.status_code, query.status_code) == (201, 200, 200)
    assert first.json() == second.json() == query.json()
    assert set(first.json()) == FIELDS
    assert first.json()["document_id"] == str(document)
    assert first.json()["publication_state"] == "CLOSED"
    for response in (first, second, query):
        assert response.headers["cache-control"] == "no-store"
    original = client.get(_url(project, key).removesuffix("/closure"))
    assert original.status_code == 200
    assert set(original.json()) == {"upload_key", "project_id", "state", "created_at", "document"}
    assert original.json()["state"] == "PENDING" and original.json()["document"] is None
    assert not fake.uploaded and not fake.deleted
    assert fake.closure_requests[0][2].request_id == UUID(first.headers["X-Request-ID"])
    assert fake.closure_requests[0][2].session_token
    assert fake.closure_queries[0][2].csrf_token == ""


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"confirmation": None},
        {"confirmation": True},
        {"confirmation": "DELETE"},
        {"confirmation": "stop_publication"},
        {**BODY, "release_quota": True},
    ],
)
def test_closure_requires_exact_explicit_confirmation(client: TestClient, body: object) -> None:
    """欠落・別操作・余剰指示を閉鎖成功へ補完しない。"""

    fake, project, key, _ = _pending(client)
    response = client.post(_url(project, key), json=body)
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert not fake.closure_requests


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize(
    "key", ["invalid", "00000000-0000-0000-0000-000000000000", "00000000000040008000000000000001"]
)
def test_invalid_closure_key_never_enters_service(
    client: TestClient,
    method: str,
    key: str,
) -> None:
    """独立 route も原 header と同じ canonical UUID 規則と静的 Problem を用いる。"""

    fake, project, _, _ = _pending(client)
    response = client.request(method, _url(project, key), json=BODY if method == "post" else None)
    assert response.status_code == 422 and response.json()["code"] == "invalid_document_upload_key"
    assert response.headers["cache-control"] == "no-store"
    assert not fake.closure_requests and not fake.closure_queries


@pytest.mark.parametrize(
    "denial", ["session", "origin", "csrf", "missing_csrf", "project", "archived"]
)
def test_closure_write_dependency_denials_never_call_service(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    denial: str,
) -> None:
    """Origin/CSRF と現在 ProjectWriteActor を迂回する閉鎖入口を作らない。"""

    fake, project, key, _ = _pending(client)
    app = _app(client)
    expected = {
        "session": 401,
        "origin": 403,
        "csrf": 403,
        "missing_csrf": 422,
        "project": 404,
        "archived": 409,
    }
    if denial == "session":
        app.state.auth_service.unauthorized = True
    elif denial == "origin":
        client.headers["Origin"] = "https://different.invalid"
    elif denial == "csrf":
        client.headers["X-CSRF-Token"] = "different-synthetic-csrf"
    elif denial == "missing_csrf":
        client.headers.pop("X-CSRF-Token")
    elif denial == "project":
        app.state.project_service = DeniedProjectAuthorizationService()
    else:
        read = app.state.project_service.get_project

        async def archived(**kwargs: object) -> object:
            """現在 Project が归档した入口拒否だけを合成する。"""

            return replace(await read(**kwargs), status=ProjectStatus.ARCHIVED)

        monkeypatch.setattr(app.state.project_service, "get_project", archived)
    response = client.post(_url(project, key), json=BODY)
    assert response.status_code == expected[denial]
    assert response.headers["cache-control"] == "no-store"
    assert not fake.closure_requests


def test_archived_closure_receipt_remains_readable_without_csrf(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档は新閉鎖を拒否するが、原回执の読取を mutation に変えない。"""

    fake, project, key, _ = _pending(client)
    first = client.post(_url(project, key), json=BODY)
    assert first.status_code == 201
    app = _app(client)
    read = app.state.project_service.get_project

    async def archived(**kwargs: object) -> object:
        """同一 Project の現在状態だけを归档へ変更する。"""

        return replace(await read(**kwargs), status=ProjectStatus.ARCHIVED)

    monkeypatch.setattr(app.state.project_service, "get_project", archived)
    client.headers.pop("X-CSRF-Token")
    response = client.get(_url(project, key))
    assert response.status_code == 200 and response.json() == first.json()
    assert len(fake.closure_requests) == 1


@pytest.mark.parametrize("method", ["get", "post"])
def test_another_admin_cannot_close_or_read_original_actor_key(
    client: TestClient,
    method: str,
) -> None:
    """Project ADMIN でも別作者 key の存在を公開せず、閉鎖権を代行しない。"""

    fake, project, key, _ = _pending(client)
    app = _app(client)
    app.state.auth_service.actor = replace(app.state.auth_service.actor, user_id=uuid4())
    response = client.request(method, _url(project, key), json=BODY if method == "post" else None)
    assert response.status_code == 404
    assert response.json()["code"] == (
        "document_upload_not_found" if method == "post" else "document_upload_closure_not_found"
    )
    assert not fake.upload_closures and not fake.deleted


@pytest.mark.parametrize(
    "method,error,expected,code",
    [
        ("post", UnauthorizedSessionError, 401, "authentication_required"),
        ("get", UnauthorizedSessionError, 401, "authentication_required"),
        ("post", CsrfRejectedError, 403, "csrf_rejected"),
        ("get", CsrfRejectedError, 403, "csrf_rejected"),
        ("post", ProjectNotFoundError, 404, "project_not_found"),
        ("get", ProjectNotFoundError, 404, "project_not_found"),
        ("post", ProjectArchivedError, 409, "project_archived"),
        ("post", DocumentUploadNotFoundError, 404, "document_upload_not_found"),
        ("get", DocumentUploadClosureNotFoundError, 404, "document_upload_closure_not_found"),
        ("post", DocumentUploadAlreadyPublishedError, 409, "document_upload_already_published"),
        ("post", DocumentUploadInvalidError, 503, "document_upload_unavailable"),
        ("get", DocumentUploadInvalidError, 503, "document_upload_unavailable"),
        *[
            (method, error, 503, "document_upload_unavailable")
            for method in ("get", "post")
            for error in (SQLAlchemyError, ConnectionError, TimeoutError)
        ],
    ],
)
def test_closure_service_denials_use_static_problem_without_private_target(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    error: type[Exception],
    expected: int,
    code: str,
) -> None:
    """入口通過後の最終拒否も元資格の意味を保ち、私有 namespace/key を漏らさない。"""

    fake, project, key, _ = _pending(client)
    monkeypatch.setattr(
        fake,
        "close_upload" if method == "post" else "get_upload_closure",
        AsyncMock(side_effect=error("private target")),
    )
    response = client.request(method, _url(project, key), json=BODY if method == "post" else None)
    assert response.status_code == expected and response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store" and "private" not in response.text
    assert not fake.deleted


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize(
    "corruption",
    ["key", "project", "document", "time", "state", "numeric_time", "string_id", "missing_time"],
)
def test_closure_response_rejects_corrupt_target_state_and_timestamp(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    corruption: str,
) -> None:
    """独立回执が valid JSON でも別 key/Project・nil ID・naive 日時・別状態を成功にしない。"""

    fake, project, key, document = _pending(client)
    stored = SimpleNamespace(
        **asdict(
            StoredDocumentUploadClosure(
                key,
                project,
                document,
                datetime.now(UTC),
            )
        )
    )
    if corruption == "key":
        stored.upload_key = uuid4()
    elif corruption == "project":
        stored.project_id = uuid4()
    elif corruption == "document":
        stored.document_id = UUID(int=0)
    elif corruption == "time":
        stored.closed_at = datetime(2026, 9, 10)
    elif corruption == "numeric_time":
        stored.closed_at = 0
    elif corruption == "string_id":
        stored.document_id = str(document)
    elif corruption == "missing_time":
        del stored.closed_at
    else:
        stored.publication_state = "CLEANED"
    monkeypatch.setattr(
        fake,
        "close_upload" if method == "post" else "get_upload_closure",
        AsyncMock(return_value=(stored, True) if method == "post" else stored),
    )
    response = client.request(method, _url(project, key), json=BODY if method == "post" else None)
    assert response.status_code == 503 and response.json()["code"] == "document_upload_unavailable"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method", ["get", "post"])
def test_closure_response_does_not_serialize_private_attributes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """内部属性が追加されても公開五項目だけを投影し、対象や監査の私有情報は含めない。"""

    fake, project, key, document = _pending(client)
    stored = SimpleNamespace(
        **asdict(StoredDocumentUploadClosure(key, project, document, datetime.now(UTC))),
        storage_key="private",
        namespace="private",
        binding_checksum="private",
        request_id="private",
        session_id="private",
        quota_bytes=5,
    )
    monkeypatch.setattr(
        fake,
        "close_upload" if method == "post" else "get_upload_closure",
        AsyncMock(return_value=(stored, True) if method == "post" else stored),
    )
    response = client.request(method, _url(project, key), json=BODY if method == "post" else None)
    assert response.status_code == (201 if method == "post" else 200)
    assert set(response.json()) == FIELDS and "private" not in response.text


def test_http_closure_uses_real_service_repository_and_preserves_pending_receipt(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実 HTTP→service→SQL fake を通し、原 intent の閉鎖と後続 POST 拒否・不変回执を検証する。"""

    app = _app(client)
    db = UploadDatabase()
    auth = app.state.auth_service
    auth.actor = db.access.actor
    auth.session_token, auth.csrf_token = db.access.session_token, db.access.csrf_token
    app.state.document_service = db.document_service
    client.cookies.set(app.state.settings.auth_session_cookie_name, db.access.session_token)
    client.headers["X-CSRF-Token"] = db.access.csrf_token
    db.storage.put.side_effect = FileStorageError("private remote unknown")
    key = uuid4()
    upload_url = f"/api/v1/projects/{db.project.id}/documents"
    upload_headers = {"Idempotency-Key": str(key)}
    first = client.post(
        upload_url, headers=upload_headers, files={"file": ("note.txt", b"hello", "text/plain")}
    )
    assert first.status_code == 503
    original_url = _url(db.project.id, key).removesuffix("/closure")
    before = client.get(original_url)
    assert before.status_code == 200 and before.json()["state"] == "PENDING"
    close = db.document_service.close_upload

    async def record_access(
        *,
        project_id: UUID,
        upload_key: UUID,
        access: UserAccess,
    ) -> tuple[StoredDocumentUploadClosure, bool]:
        """HTTP middleware の今回 request ID を SQL fake の監査期待値にも渡す。"""

        db.access = access
        return await close(project_id=project_id, upload_key=upload_key, access=access)

    monkeypatch.setattr(db.document_service, "close_upload", record_access)
    stopped = client.post(_url(db.project.id, key), json=BODY)
    assert stopped.status_code == 201
    repeated = client.post(_url(db.project.id, key), json=BODY)
    assert repeated.status_code == 200 and repeated.json() == stopped.json()
    queried = client.get(_url(db.project.id, key))
    assert queried.status_code == 200 and queried.json() == stopped.json()
    assert client.get(original_url).json() == before.json()
    retry = client.post(
        upload_url, headers=upload_headers, files={"file": ("note.txt", b"hello", "text/plain")}
    )
    assert retry.status_code == 409 and retry.json()["code"] == "document_upload_closed"
    assert len(db.intents) == len(db.closures) == 1 and not db.documents
    assert db.closures[0].request_id == UUID(stopped.headers["X-Request-ID"])
    assert db.closures[0].request_id != db.intents[0].original_request_id
    assert db.storage.put.await_count == 1
    db.storage.delete.assert_not_called()
