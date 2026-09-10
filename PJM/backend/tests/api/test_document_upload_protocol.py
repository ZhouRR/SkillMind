"""原 upload key の接收前検証・持続投影・再送拒否を実 HTTP 境界で検証する。"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fakes import DeniedProjectAuthorizationService, FakeDocumentService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.documents.domain import (
    DocumentUploadInvalidError,
    DocumentUploadKeyConflictError,
    DocumentUploadNotFoundError,
    DocumentUploadPendingError,
    StoredDocument,
    StoredDocumentUpload,
)
from projectmind.projects.domain import ProjectNotFoundError, ProjectStatus
from projectmind.storage import FileStorageError
from projectmind.users.domain import UserAccess
from tests.documents.upload_harness import UploadDatabase

_DOCUMENT_FIELDS = {
    "document_id", "project_id", "folder", "name", "size", "mime", "checksum",
    "uploaded_by", "created_at",
}
_UPLOAD_FIELDS = {"upload_key", "project_id", "state", "created_at", "document"}


def _app(client: TestClient) -> FastAPI:
    """TestClient の汎用 ASGI 型を実 app と確認して state に接続する。"""

    assert isinstance(client.app, FastAPI)
    return client.app


def _document(project_id: UUID, actor_id: UUID) -> StoredDocument:
    """元公開 metadata を合成し、現在の文書一覧や bucket を参照しない。"""

    return StoredDocument(
        document_id=uuid4(), project_id=project_id, folder="specs", name="note.txt",
        size=4, mime="text/plain", checksum="sha256:" + "a" * 64, uploaded_by=actor_id,
        created_at=datetime(2026, 9, 10, tzinfo=UTC),
    )


@pytest.mark.parametrize("values", [
    [], [b""], [b"invalid"], [b"00000000-0000-0000-0000-000000000000"],
    [b"00000000000040008000000000000001"],
    [b" 00000000-0000-4000-8000-000000000001"],
    [b"00000000-0000-4000-8000-000000000001", b"00000000-0000-4000-8000-000000000001"],
    [b"00000000-0000-4000-8000-000000000001,00000000-0000-4000-8000-000000000002"],
])
async def test_bad_upload_key_is_rejected_before_actual_asgi_receive(
    client: TestClient, values: list[bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """認可済みでも原 key 不正なら本文に触れず、service 呼出しも開始しない。"""

    app = client.app
    assert isinstance(app, FastAPI)
    fake = FakeDocumentService()
    upload = AsyncMock(side_effect=AssertionError("Invalid key must not start upload"))
    monkeypatch.setattr(fake, "upload_document", upload)
    app.state.document_service = fake
    scope: Scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "POST", "scheme": "http", "root_path": "",
        "path": f"/api/v1/projects/{uuid4()}/documents", "query_string": b"",
        "headers": [(b"origin", b"http://testserver"),
                    (b"x-csrf-token", app.state.auth_service.csrf_token.encode()),
                    (b"content-type", b"multipart/form-data; boundary=never-read"),
                    *((b"idempotency-key", value) for value in values)],
        "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
    }
    receive = AsyncMock(side_effect=AssertionError("Invalid key must precede body reads"))
    messages: list[Message] = []

    async def send(message: Message) -> None:
        """実 app が返した固定拒否と cache header を観測する。"""

        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = json.loads(b"".join(message.get("body", b"") for message in messages))
    assert start["status"] == 422 and body["code"] == "invalid_document_upload_key"
    assert (b"cache-control", b"no-store") in start["headers"]
    receive.assert_not_called()
    upload.assert_not_called()


def test_same_original_key_replays_metadata_without_a_second_upload(client: TestClient) -> None:
    """同じ原内容/key の HTTP 再送は元九項目を返し、fake の保存回数も増えない。"""

    fake = FakeDocumentService()
    _app(client).state.document_service = fake
    project_id, upload_key = uuid4(), uuid4()
    url = f"/api/v1/projects/{project_id}/documents"
    headers = {"Idempotency-Key": str(upload_key).upper()}
    first = client.post(url, headers=headers, files={"file": ("note.txt", b"note", "text/plain")})
    second = client.post(url, headers=headers, files={"file": ("note.txt", b"note", "text/plain")})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() and set(first.json()) == _DOCUMENT_FIELDS
    assert len(fake.uploaded) == 1 and len(fake.uploads) == 1
    assert first.headers["cache-control"] == second.headers["cache-control"] == "no-store"


def test_same_key_with_different_content_is_a_static_conflict(client: TestClient) -> None:
    """fingerprint 差分を同名競合と混同せず、原 upload を上書きしない。"""

    fake = FakeDocumentService()
    _app(client).state.document_service = fake
    url = f"/api/v1/projects/{uuid4()}/documents"
    headers = {"Idempotency-Key": str(uuid4())}
    first = client.post(url, headers=headers, files={"file": ("note.txt", b"one", "text/plain")})
    assert first.status_code == 201
    response = client.post(url, headers=headers, files={"file": ("note.txt", b"two", "text/plain")})
    assert response.status_code == 409 and response.json()["code"] == "document_upload_key_conflict"
    assert response.headers["cache-control"] == "no-store" and "private" not in response.text
    assert len(fake.uploaded) == 1


@pytest.mark.parametrize("error,status,code", [
    (DocumentUploadKeyConflictError, 409, "document_upload_key_conflict"),
    (DocumentUploadPendingError, 409, "document_upload_pending"),
    (DocumentUploadInvalidError, 503, "document_upload_unavailable"),
])
def test_upload_protocol_errors_keep_static_problems(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: type[Exception],
    status: int, code: str,
) -> None:
    """既存意図の未知/損傷を未保存の成功へ変換せず、内部本文を公開しない。"""

    fake = FakeDocumentService()
    monkeypatch.setattr(fake, "upload_document", AsyncMock(side_effect=error("private state")))
    _app(client).state.document_service = fake
    response = client.post(
        f"/api/v1/projects/{uuid4()}/documents", headers={"Idempotency-Key": str(uuid4())},
        files={"file": ("note.txt", b"note", "text/plain")},
    )
    assert response.status_code == status and response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store" and "private" not in response.text


@pytest.mark.parametrize("published", [False, True])
def test_original_upload_query_projects_only_receipt_fields(
    client: TestClient, published: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PENDING/null と PUBLISHED/原文書を区別し、照合 GET で本文・PUT・DELETE を呼ばない。"""

    app = client.app
    assert isinstance(app, FastAPI)
    fake = FakeDocumentService()
    app.state.document_service = fake
    project_id, upload_key = uuid4(), uuid4()
    actor_id = app.state.auth_service.actor.user_id
    stored = StoredDocumentUpload(
        upload_key, project_id, "PUBLISHED" if published else "PENDING",
        datetime(2026, 9, 10, tzinfo=UTC), _document(project_id, actor_id) if published else None,
    )
    fake.uploads[(project_id, actor_id, upload_key)] = stored
    for method in ("upload_document", "download_document", "delete_document"):
        monkeypatch.setattr(fake, method, AsyncMock(side_effect=AssertionError("Read only")))
    client.headers.pop("X-CSRF-Token")
    response = client.get(f"/api/v1/projects/{project_id}/document-uploads/{upload_key}")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == _UPLOAD_FIELDS
    assert body["upload_key"] == str(upload_key) and body["project_id"] == str(project_id)
    assert body["state"] == stored.state
    if published:
        assert set(body["document"]) == _DOCUMENT_FIELDS
    else:
        assert body["document"] is None
    assert len(fake.upload_queries) == 1
    assert fake.upload_queries[0][2].actor.user_id == actor_id
    assert fake.upload_queries[0][2].csrf_token == ""


def test_published_receipt_survives_document_deletion(client: TestClient) -> None:
    """公開済み原記録を現在の一覧照合へ置換せず、削除後も同じ九項目を返す。"""

    fake = FakeDocumentService()
    _app(client).state.document_service = fake
    project_id, upload_key = uuid4(), uuid4()
    created = client.post(
        f"/api/v1/projects/{project_id}/documents", headers={"Idempotency-Key": str(upload_key)},
        files={"file": ("note.txt", b"note", "text/plain")},
    )
    assert created.status_code == 201
    document_id = created.json()["document_id"]
    deleted = client.delete(f"/api/v1/projects/{project_id}/documents/{document_id}")
    assert deleted.status_code == 204
    fake.not_found = True
    assert client.get(f"/api/v1/projects/{project_id}/documents/{document_id}").status_code == 404
    response = client.get(f"/api/v1/projects/{project_id}/document-uploads/{upload_key}")
    assert response.status_code == 200 and response.json()["document"] == created.json()
    assert len(fake.uploaded) == 1


def test_original_upload_query_requires_same_current_actor(client: TestClient) -> None:
    """同一 Project に認可されても別 actor の原 upload key を閲覧しない。"""

    app = client.app
    assert isinstance(app, FastAPI)
    fake = FakeDocumentService()
    app.state.document_service = fake
    project_id, upload_key = uuid4(), uuid4()
    identity = (project_id, app.state.auth_service.actor.user_id, upload_key)
    fake.uploads[identity] = StoredDocumentUpload(
        upload_key, project_id, "PENDING", datetime(2026, 9, 10, tzinfo=UTC), None
    )
    app.state.auth_service.actor = replace(app.state.auth_service.actor, user_id=uuid4())
    response = client.get(f"/api/v1/projects/{project_id}/document-uploads/{upload_key}")
    assert response.status_code == 404 and response.json()["code"] == "document_upload_not_found"
    assert response.headers["cache-control"] == "no-store"


def test_archived_project_allows_read_only_original_upload_confirmation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档は原 upload の GET を妨げず、確認時に新しい書込権を発行しない。"""

    app = client.app
    assert isinstance(app, FastAPI)
    fake = FakeDocumentService()
    app.state.document_service = fake
    project_id, upload_key = uuid4(), uuid4()
    identity = (project_id, app.state.auth_service.actor.user_id, upload_key)
    fake.uploads[identity] = StoredDocumentUpload(
        upload_key, project_id, "PENDING", datetime(2026, 9, 10, tzinfo=UTC), None
    )
    original = app.state.project_service.get_project

    async def archived(**kwargs: object) -> object:
        """既存 ProjectReadActor に同じ Project の归档状態だけを返す。"""

        return replace(await original(**kwargs), status=ProjectStatus.ARCHIVED)

    monkeypatch.setattr(app.state.project_service, "get_project", archived)
    response = client.get(f"/api/v1/projects/{project_id}/document-uploads/{upload_key}")
    assert response.status_code == 200 and response.json()["state"] == "PENDING"
    assert not fake.uploaded


@pytest.mark.parametrize("error,status,code", [
    (UnauthorizedSessionError, 401, "authentication_required"),
    (CsrfRejectedError, 403, "csrf_rejected"),
    (ProjectNotFoundError, 404, "project_not_found"),
    (DocumentUploadNotFoundError, 404, "document_upload_not_found"),
    (DocumentUploadInvalidError, 503, "document_upload_unavailable"),
    (FileStorageError, 503, "document_upload_unavailable"),
])
def test_original_upload_query_maps_stable_service_refusals(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: type[Exception],
    status: int, code: str,
) -> None:
    """現在資格と原記録の拒否を区別し、namespace/key/内部 receipt を公開しない。"""

    fake = FakeDocumentService()
    monkeypatch.setattr(fake, "get_upload", AsyncMock(side_effect=error("private receipt")))
    _app(client).state.document_service = fake
    response = client.get(f"/api/v1/projects/{uuid4()}/document-uploads/{uuid4()}")
    assert response.status_code == status and response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store" and "private" not in response.text


@pytest.mark.parametrize("denial", ["session", "project"])
def test_query_authorization_failure_never_queries_receipt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, denial: str,
) -> None:
    """入口で越权/不存在を拒否し、原 upload の存在照合にも進まない。"""

    app = client.app
    assert isinstance(app, FastAPI)
    fake = FakeDocumentService()
    operation = AsyncMock(side_effect=AssertionError("Unauthorized receipt query"))
    monkeypatch.setattr(fake, "get_upload", operation)
    app.state.document_service = fake
    if denial == "session":
        app.state.auth_service.unauthorized = True
    else:
        app.state.project_service = DeniedProjectAuthorizationService()
    response = client.get(f"/api/v1/projects/{uuid4()}/document-uploads/{uuid4()}")
    assert response.status_code == (401 if denial == "session" else 404)
    operation.assert_not_called()


@pytest.mark.parametrize("corruption", ["key", "project", "state", "missing_document",
                                        "pending_document", "document_project", "naive_time"])
def test_inconsistent_receipt_never_becomes_a_successful_projection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, corruption: str,
) -> None:
    """有効型に見える持続投影も原対象・状態・日時を照合し、損傷は固定 503 に閉じる。"""

    app = client.app
    assert isinstance(app, FastAPI)
    project_id, upload_key = uuid4(), uuid4()
    document = _document(project_id, app.state.auth_service.actor.user_id)
    original = StoredDocumentUpload(
        upload_key, project_id, "PUBLISHED", document.created_at, document,
    )
    stored: StoredDocumentUpload | SimpleNamespace
    if corruption == "key":
        stored = replace(original, upload_key=uuid4())
    elif corruption == "project":
        stored = replace(original, project_id=uuid4())
    elif corruption == "state":
        stored = SimpleNamespace(**asdict(original))
        stored.state = "UNKNOWN"
        stored.document = document
    elif corruption == "missing_document":
        stored = replace(original, document=None)
    elif corruption == "pending_document":
        stored = replace(original, state="PENDING")
    elif corruption == "document_project":
        stored = replace(original, document=replace(document, project_id=uuid4()))
    else:
        stored = replace(original, created_at=datetime(2026, 9, 10))
    fake = FakeDocumentService()
    monkeypatch.setattr(fake, "get_upload", AsyncMock(return_value=stored))
    app.state.document_service = fake
    response = client.get(f"/api/v1/projects/{project_id}/document-uploads/{upload_key}")
    assert response.status_code == 503 and response.json()["code"] == "document_upload_unavailable"
    assert response.headers["cache-control"] == "no-store"


def test_receipt_projection_never_serializes_additional_internal_attributes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """service DTO に内部属性が増えても外層五項目・文書九項目だけを公開する。"""

    app = client.app
    assert isinstance(app, FastAPI)
    project_id, upload_key = uuid4(), uuid4()
    document = _document(project_id, app.state.auth_service.actor.user_id)
    hidden_document = SimpleNamespace(**asdict(document), storage_key="private", quota_bytes=4)
    stored = SimpleNamespace(
        upload_key=upload_key, project_id=project_id, state="PUBLISHED",
        created_at=document.created_at, document=hidden_document,
        namespace="private", fingerprint="private", session_token="private",
    )
    fake = FakeDocumentService()
    monkeypatch.setattr(fake, "get_upload", AsyncMock(return_value=stored))
    app.state.document_service = fake
    response = client.get(f"/api/v1/projects/{project_id}/document-uploads/{upload_key}")
    assert response.status_code == 200 and "private" not in response.text
    assert set(response.json()) == _UPLOAD_FIELDS
    assert set(response.json()["document"]) == _DOCUMENT_FIELDS


def test_upload_protocol_openapi_preserves_manual_body_and_required_header(
    client: TestClient,
) -> None:
    """header と原確認 route の追加で、multipart 接收前門禁や Problem 契約を隠さない。"""

    paths = _app(client).openapi()["paths"]
    upload = paths["/api/v1/projects/{project_id}/documents"]["post"]
    header = next(item for item in upload["parameters"] if item["name"] == "Idempotency-Key")
    assert header["in"] == "header" and header["required"] is True
    assert header["schema"]["format"] == "uuid"
    assert "multipart/form-data" in upload["requestBody"]["content"]
    query = paths["/api/v1/projects/{project_id}/document-uploads/{upload_key}"]["get"]
    for operation, statuses in ((upload, (409, 422, 503)), (query, (401, 403, 404, 422, 503))):
        for status in statuses:
            response = operation["responses"][str(status)]
            assert "application/problem+json" in response["content"]
            assert "Cache-Control" in response["headers"]
    assert "subsequently deleted" in query["responses"]["200"]["description"]


@pytest.mark.parametrize("storage_unknown", [False, True])
def test_http_upload_receipt_connects_to_persistent_service_protocol(
    client: TestClient, storage_unknown: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実 app/service/repository と SQL fake を結び、GET と再送が PUT を増やさない。"""

    app = _app(client)
    db = UploadDatabase()
    auth = app.state.auth_service
    auth.actor = db.access.actor
    auth.session_token, auth.csrf_token = db.access.session_token, db.access.csrf_token
    app.state.document_service = db.document_service
    client.cookies.set(app.state.settings.auth_session_cookie_name, db.access.session_token)
    if storage_unknown:
        db.storage.put.side_effect = FileStorageError("private remote outcome")
    upload_key = uuid4()
    url = f"/api/v1/projects/{db.project.id}/documents"
    headers = {"Idempotency-Key": str(upload_key), "X-CSRF-Token": db.access.csrf_token}
    first = client.post(url, headers=headers, files={"file": ("note.txt", b"hello", "text/plain")})
    assert first.status_code == (503 if storage_unknown else 201)
    query_url = f"/api/v1/projects/{db.project.id}/document-uploads/{upload_key}"
    for _ in range(2):
        queried = client.get(query_url)
        assert queried.status_code == 200 and queried.headers["cache-control"] == "no-store"
        receipt = queried.json()
        assert receipt["state"] == ("PENDING" if storage_unknown else "PUBLISHED")
        assert receipt["document"] == (None if storage_unknown else first.json())
        assert set(receipt) == _UPLOAD_FIELDS
    replay = client.post(url, headers=headers, files={"file": ("note.txt", b"hello", "text/plain")})
    assert replay.status_code == (409 if storage_unknown else 201)
    if storage_unknown:
        assert replay.json()["code"] == "document_upload_pending"
    else:
        assert replay.json() == first.json()
        original_delete = db.document_service.delete_document

        async def record_delete_access(
            *, project_id: UUID, document_id: UUID, access: UserAccess,
        ) -> None:
            """middleware が生成した今回の request ID を、SQL fake の期待資格にも渡す。"""

            assert access.actor == db.access.actor
            assert access.session_token == db.access.session_token
            assert access.csrf_token == db.access.csrf_token
            db.access = access
            await original_delete(project_id=project_id, document_id=document_id, access=access)

        monkeypatch.setattr(db.document_service, "delete_document", record_delete_access)
        deleted = client.delete(
            f"{url}/{first.json()['document_id']}",
            headers={"X-CSRF-Token": db.access.csrf_token},
        )
        assert deleted.status_code == 204 and not db.documents
        assert len(db.cleanups) == 1
        assert db.cleanups[0].request_id == UUID(deleted.headers["X-Request-ID"])
        assert db.cleanups[0].request_id != db.intents[0].original_request_id
        after_delete = client.get(query_url)
        assert after_delete.status_code == 200 and after_delete.json()["document"] == first.json()
    assert len(db.intents) == 1 and db.storage.put.await_count == 1
