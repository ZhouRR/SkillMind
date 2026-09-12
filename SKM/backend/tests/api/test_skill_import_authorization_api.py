"""Skill import HTTP を原資格/実 service/SQL seam へ接続し、実 DB の証明と区別する。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from starlette.types import Message

from skillmind.api.problems import PROBLEM_DETAILS_SCHEMA
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.skills import SkillStorageUnavailableError
from skillmind.users.domain import UserAccess, UserAdministrationDeniedError
from tests.api.fakes import FakeAuthService, FakeSkillService
from tests.api.test_skill_upload_stream import CONTENT_TYPE, END, file_part, part, scope
from tests.skills.skill_import_authorization_harness import (
    INLINE_FILES,
    UPLOAD_FILES,
    ImportKind,
    ImportSession,
)


def application(client: TestClient) -> FastAPI:
    """共有 client の既存 lifespan だけを使い、追加接続を作らない。"""

    assert isinstance(client.app, FastAPI)
    return client.app


def connect(client: TestClient, session: ImportSession) -> None:
    """入口認証だけを fake に保ち、事業 transaction の実資格判定を差し替えない。"""

    app = application(client)
    auth = app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    auth.actor = session.access.actor
    auth.session_token = session.access.session_token
    auth.csrf_token = session.access.csrf_token
    client.cookies.set(app.state.settings.auth_session_cookie_name, auth.session_token)
    client.headers["X-CSRF-Token"] = auth.csrf_token
    app.state.skill_service = session.service()


@pytest.mark.parametrize("denial", ["session", "origin", "csrf", "missing-csrf", "role"])
async def test_skill_upload_authentication_happens_before_any_asgi_body_read(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    denial: str,
) -> None:
    """File/Form 事前処理がなく、未認証の巨大/未完本文でも receive と保存はゼロになる。"""

    app = application(client)
    auth = app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    fake = FakeSkillService()
    writer = AsyncMock(side_effect=AssertionError("Rejected actor must not save sources"))
    monkeypatch.setattr(fake, "save_upload", writer)
    app.state.skill_service = fake
    origin, csrf = b"http://testserver", auth.csrf_token.encode()
    if denial == "session":
        auth.unauthorized = True
    elif denial == "origin":
        origin = b"https://synthetic-other.invalid"
    elif denial == "csrf":
        csrf = b"synthetic-wrong-csrf"
    elif denial == "role":
        auth.actor = replace(auth.actor, system_role="USER")
    headers = [(b"content-type", CONTENT_TYPE), (b"origin", origin)]
    if denial != "missing-csrf":
        headers.append((b"x-csrf-token", csrf))
    receive = AsyncMock(side_effect=AssertionError("Authorization must precede body reads"))
    messages: list[Message] = []

    async def send(message: Message) -> None:
        """実 app の HTTP status と公開 Problem を収集する。"""

        messages.append(message)

    await app(scope(headers), receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    assert start["status"] == (
        401 if denial == "session" else 422 if denial == "missing-csrf" else 403
    )
    receive.assert_not_called()
    writer.assert_not_called()


@pytest.mark.parametrize("revoke_during_receive", [False, True])
async def test_skill_upload_real_asgi_rechecks_original_session_after_receiving_source(
    client: TestClient,
    revoke_during_receive: bool,
) -> None:
    """実 route→原 access→実 service/SQL を通し、接收中の撤権で PUT/保存を開始しない。"""

    session = ImportSession()
    connect(client, session)
    app = application(client)
    incoming = b"".join(file_part(file.data, file.path.encode()) for file in UPLOAD_FILES) + END
    received: list[bool] = []
    messages: list[Message] = []

    async def receive() -> Message:
        """入口を通過した後だけ原 session を失効させ、本文を一度渡す。"""

        assert received == [] and session.events == []
        received.append(True)
        if revoke_during_receive:
            session.auth_session.revoked_at = datetime.now(UTC)
        return {"type": "http.request", "body": incoming, "more_body": False}

    async def send(message: Message) -> None:
        """成功/拒否の raw response を収集し、内部 body の漏洩を検査する。"""

        messages.append(message)

    await app(
        scope(
            [
                (b"content-type", CONTENT_TYPE),
                (b"origin", b"http://testserver"),
                (b"x-csrf-token", session.access.csrf_token.encode()),
                (
                    b"cookie",
                    (
                        app.state.settings.auth_session_cookie_name
                        + "="
                        + session.access.session_token
                    ).encode(),
                ),
            ]
        ),
        receive,
        send,
    )
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"") for item in messages if item["type"] == "http.response.body"
    )
    payload = json.loads(body)
    assert start["status"] == (401 if revoke_during_receive else 201)
    assert len(session.sources) == len(session.interpretations) == int(not revoke_during_receive)
    assert len(session.storage.attempts) == (0 if revoke_during_receive else len(UPLOAD_FILES))
    assert session.storage.deletions == []
    assert session.access.session_token.encode() not in body
    assert session.access.csrf_token.encode() not in body
    if revoke_during_receive:
        assert payload["code"] == "authentication_required"
    else:
        assert payload["organization_id"] == str(session.access.actor.organization_id)
        assert payload["skill_source_id"] == str(session.sources[0].id)
        assert payload["interpretation_id"] == str(session.interpretations[0].id)


@pytest.mark.parametrize("kind", ["inline", "upload"])
@pytest.mark.parametrize(
    "failure,status,code",
    [
        ("revoked", 401, "authentication_required"),
        ("role", 403, "administrator_required"),
        ("csrf", 403, "csrf_rejected"),
    ],
)
def test_skill_import_http_maps_real_business_authority_refusal(
    client: TestClient,
    kind: ImportKind,
    failure: str,
    status: int,
    code: str,
) -> None:
    """有効に見えた入口の後でも、原資格/現 ADMIN は実 transaction で判定する。"""

    session = ImportSession()
    connect(client, session)
    if failure == "revoked":
        session.auth_session.revoked_at = datetime.now(UTC)
    elif failure == "role":
        session.user.system_role = "USER"
        session.auth_session.system_role_at_login = "USER"
    else:
        auth = application(client).state.auth_service
        assert isinstance(auth, FakeAuthService)
        auth.csrf_token = "synthetic-other-csrf"
        client.headers["X-CSRF-Token"] = auth.csrf_token
    if kind == "inline":
        response = client.post(
            "/api/v1/skill-imports",
            json={
                "files": [{"path": file.path, "content": file.content} for file in INLINE_FILES],
            },
        )
    else:
        response = client.post(
            "/api/v1/skill-imports/upload",
            files=[("files", (file.path, file.data, file.content_type)) for file in UPLOAD_FILES],
        )
    assert response.status_code == status and response.json()["code"] == code
    assert session.sources == [] and session.interpretations == []
    assert session.storage.attempts == [] and session.storage.deletions == []
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("kind", ["inline", "upload"])
@pytest.mark.parametrize(
    "error_type,status,code",
    [
        (UnauthorizedSessionError, 401, "authentication_required"),
        (CsrfRejectedError, 403, "csrf_rejected"),
        (UserAdministrationDeniedError, 403, "administrator_required"),
        (SkillStorageUnavailableError, 503, "skill_storage_unavailable"),
    ],
)
def test_skill_import_http_keeps_original_access_and_static_shared_problems(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    error_type: type[Exception],
    status: int,
    code: str,
) -> None:
    """mapper の全分岐は原 token/request ID を内部だけへ渡し、例外詳細を公開しない。"""

    app = application(client)
    auth = app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    client.cookies.set(app.state.settings.auth_session_cookie_name, auth.session_token)
    fake = FakeSkillService()
    marker = "Synthetic private source and storage detail"
    writer = AsyncMock(side_effect=error_type(marker))
    monkeypatch.setattr(fake, f"save_{kind}", writer)
    app.state.skill_service = fake
    if kind == "inline":
        response = client.post(
            "/api/v1/skill-imports",
            json={
                "files": [{"path": "SKILL.md", "content": "# Synthetic Skill\n"}],
            },
        )
    else:
        response = client.post(
            "/api/v1/skill-imports/upload",
            files=[
                ("files", ("SKILL.md", b"# Synthetic Skill\n", "text/markdown")),
            ],
        )
    assert response.status_code == status and response.json()["code"] == code
    assert marker not in response.text
    writer.assert_awaited_once()
    assert writer.await_args is not None
    kwargs = writer.await_args.kwargs
    assert set(kwargs) == {"access", "files"}
    access = kwargs["access"]
    assert isinstance(access, UserAccess)
    assert access.actor == auth.actor
    assert access.session_token == auth.session_token
    assert access.csrf_token == auth.csrf_token
    assert str(access.request_id) == response.headers["x-request-id"]
    assert access.session_token not in response.text and access.csrf_token not in response.text


@pytest.mark.parametrize("kind", ["inline", "upload"])
@pytest.mark.parametrize("failure", ["db", "connection", "timeout"])
def test_skill_import_http_never_calls_unknown_database_commit_a_storage_refusal_or_retries(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    failure: str,
) -> None:
    """未処理 commit/接続例外は原型で伝わり、確定した storage 503 や自動再送へ変換しない。"""

    error = (
        OperationalError("synthetic SQL", None, RuntimeError("synthetic unknown commit"))
        if failure == "db"
        else ConnectionError("synthetic connection")
        if failure == "connection"
        else TimeoutError("synthetic timeout")
    )
    fake = FakeSkillService()
    writer = AsyncMock(side_effect=error)
    monkeypatch.setattr(fake, f"save_{kind}", writer)
    application(client).state.skill_service = fake
    with pytest.raises(type(error)) as observed:
        if kind == "inline":
            client.post(
                "/api/v1/skill-imports", json={"files": [{"path": "SKILL.md", "content": "x"}]}
            )
        else:
            client.post("/api/v1/skill-imports/upload", files=[("files", ("SKILL.md", b"x"))])
    assert observed.value is error
    writer.assert_awaited_once()


@pytest.mark.parametrize(
    "filename",
    [
        b"../SKILL.md",
        b"/SKILL.md",
        b"C:/folder/SKILL.md",
        rb"C:\folder\SKILL.md",
        rb"\\server\share\SKILL.md",
        b"folder/../SKILL.md",
        b"folder/./SKILL.md",
    ],
)
def test_skill_upload_http_delegates_original_unsafe_path_to_shared_parser(
    client: TestClient,
    filename: bytes,
) -> None:
    """相対 path 違反は従来 parser の 400 で拒否し、basename 補正や blob 保存はしない。"""

    session = ImportSession()
    connect(client, session)
    wire_name = filename.replace(b"\\", b"\\\\")
    response = client.post(
        "/api/v1/skill-imports/upload",
        content=file_part(filename=wire_name) + END,
        headers={"Content-Type": CONTENT_TYPE.decode()},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_file_path"
    assert session.storage.attempts == [] and session.sources == []


@pytest.mark.parametrize(
    "kind,paths",
    [
        pytest.param("inline", ("界" * 86 + ".txt",), id="inline-utf8-name"),
        pytest.param("upload", ("界" * 86 + ".txt",), id="upload-utf8-name"),
        pytest.param("upload", ("x" * 260,), id="upload-ascii-name"),
        pytest.param("inline", ("a", "a/b.txt"), id="inline-file-before-directory"),
        pytest.param("inline", ("a/b.txt", "a"), id="inline-directory-before-file"),
        pytest.param("upload", ("a", "a/b.txt"), id="upload-file-before-directory"),
        pytest.param("upload", ("a/b.txt", "a"), id="upload-directory-before-file"),
    ],
)
def test_skill_import_http_rejects_unmaterializable_paths_before_database_or_put(
    client: TestClient,
    kind: ImportKind,
    paths: tuple[str, ...],
) -> None:
    """文字数内の UTF-8 名と両順序の衝突も、実一時展開で静的 400 として拒否する。"""

    session = ImportSession()
    connect(client, session)
    marker = "Synthetic private source body"
    if kind == "inline":
        assert all(len(path) <= 240 for path in paths)
        response = client.post(
            "/api/v1/skill-imports",
            json={
                "files": [
                    {"path": INLINE_FILES[0].path, "content": INLINE_FILES[0].content},
                    *({"path": path, "content": marker} for path in paths),
                ],
            },
        )
    else:
        response = client.post(
            "/api/v1/skill-imports/upload",
            content=(
                file_part(UPLOAD_FILES[0].data, UPLOAD_FILES[0].path.encode())
                + b"".join(file_part(marker.encode(), path.encode()) for path in paths)
                + END
            ),
            headers={"Content-Type": CONTENT_TYPE.decode()},
        )
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == "invalid_file_path"
    assert payload["detail"] == "Skill source contains a path that cannot be materialized"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert marker not in response.text and "/tmp/" not in response.text
    assert "skillmind-skill-" not in response.text
    assert session.events == [] and session.timeline == []
    assert session.sources == [] and session.interpretations == []
    assert session.storage.attempts == [] and session.storage.deletions == []


@pytest.mark.parametrize("kind", ["inline", "upload"])
def test_skill_import_openapi_retains_201_identity_and_declares_real_problems(
    client: TestClient,
    kind: Literal["inline", "upload"],
) -> None:
    """手動 multipart でも公開 files 配列を残し、認証/容量/構造/保存拒否を明示する。"""

    path = "/api/v1/skill-imports" + ("/upload" if kind == "upload" else "")
    operation = application(client).openapi()["paths"][path]["post"]
    assert "201" in operation["responses"]
    statuses = [400, 401, 403, 422, 503] + ([413] if kind == "upload" else [])
    for status in statuses:
        declaration = operation["responses"][str(status)]
        assert declaration["content"] == {
            "application/problem+json": {"schema": PROBLEM_DETAILS_SCHEMA},
        }
        assert "Cache-Control" not in declaration.get("headers", {})
    if kind == "upload":
        schema = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
        assert schema["required"] == ["files"] and schema["additionalProperties"] is False
        assert schema["properties"]["files"] == {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {"type": "string", "format": "binary"},
        }


@pytest.mark.parametrize(
    "body,status,code",
    [
        (file_part(b"x" * 1_000_001) + END, 413, "skill_upload_too_large"),
        (
            b"".join(file_part(b"", str(index).encode()) for index in range(101)) + END,
            413,
            "skill_upload_too_large",
        ),
        (file_part(), 422, "invalid_skill_upload"),
        (part(b'form-data; name="other"', b"private source") + END, 422, "invalid_skill_upload"),
    ],
)
def test_skill_upload_http_rejects_receiving_failures_before_saving(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    status: int,
    code: str,
) -> None:
    """実 route の容量/構造拒否では import service を開始せず、元本文を Problem に入れない。"""

    fake = FakeSkillService()
    writer = AsyncMock(side_effect=AssertionError("Rejected body must not start persistence"))
    monkeypatch.setattr(fake, "save_upload", writer)
    application(client).state.skill_service = fake
    response = client.post(
        "/api/v1/skill-imports/upload",
        content=body,
        headers={"Content-Type": CONTENT_TYPE.decode()},
    )
    assert response.status_code == status and response.json()["code"] == code
    assert "private source" not in response.text
    writer.assert_not_called()


async def test_skill_upload_actual_asgi_cancellation_does_not_finish_or_save(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """認証済み app の部分受信 Task を取消しても、201 や保存開始へ変換しない。"""

    app = application(client)
    auth = app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    fake = FakeSkillService()
    writer = AsyncMock(side_effect=AssertionError("Cancelled upload must not save"))
    monkeypatch.setattr(fake, "save_upload", writer)
    app.state.skill_service = fake
    ready = asyncio.Event()
    calls = 0
    messages: list[Message] = []

    async def receive() -> Message:
        """一部本文の後、実 Task.cancel によって終了する受信待ちを作る。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": file_part(), "more_body": True}
        ready.set()
        await asyncio.Future[None]()
        raise AssertionError("Cancelled receive unexpectedly resumed")

    async def send(message: Message) -> None:
        """中断前に成功応答が先送信されていないことを観測する。"""

        messages.append(message)

    task = asyncio.create_task(
        app(
            scope(
                [
                    (b"content-type", CONTENT_TYPE),
                    (b"origin", b"http://testserver"),
                    (b"x-csrf-token", auth.csrf_token.encode()),
                ]
            ),
            receive,
            send,
        )
    )
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done() and messages == []
    writer.assert_not_called()


@pytest.mark.parametrize("operation", ["interpret", "adjust"])
def test_import_storage_problem_factory_remains_static_for_existing_consumers(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """単一 factory の安全な detail は既存 interpret/adjust でも同じ 503/code を維持する。"""

    fake = FakeSkillService()
    marker = "Synthetic private storage connection detail"
    writer = AsyncMock(side_effect=SkillStorageUnavailableError(marker))
    monkeypatch.setattr(fake, "accept_interpretation_request", writer)
    application(client).state.skill_service = fake
    if operation == "interpret":
        response = client.post(
            f"/api/v1/skill-sources/{fake.stored.skill_source_id}/interpretation-requests",
            json={"request_id": str(uuid4())},
        )
    else:
        response = client.post(
            f"/api/v1/skill-interpretations/{fake.stored.interpretation_id}/adjustment-requests",
            json={"request_id": str(uuid4()), "instruction": "Synthetic adjustment"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "skill_storage_unavailable"
    assert response.json()["detail"] == "The Skill source storage is unavailable."
    assert marker not in response.text
    writer.assert_awaited_once()
