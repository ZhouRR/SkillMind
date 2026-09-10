"""認証前の受信拒否と有界 multipart、原 credential の service 接続を検証する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import anyio
import pytest
from fakes import DeniedProjectAuthorizationService, FakeDocumentService
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from python_multipart import MultipartParser
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

from projectmind.api import document_upload
from projectmind.api.document_upload import MULTIPART_OVERHEAD_BYTES, read_document_upload
from projectmind.api.problems import ProblemException
from projectmind.auth.domain import generate_session_credentials
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.documents.domain import DocumentConflictError
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError, ProjectStatus
from projectmind.users.domain import UserAccess
from tests.documents.upload_harness import UploadDatabase

_CONTENT_TYPE = b"multipart/form-data; boundary=bounded-upload"


def _part(disposition: bytes, data: bytes, extra_headers: bytes = b"") -> bytes:
    """合成 multipart part を作り、実 file や個人データは使わない。"""

    return (
        b"--bounded-upload\r\nContent-Disposition: " + disposition + b"\r\n"
        + extra_headers + b"\r\n" + data + b"\r\n"
    )


def _body(data: bytes = b"body", *, folder: bytes | None = None) -> bytes:
    """通常 browser と同じ固定二 field 以下の UTF-8 multipart を作る。"""

    prefix = b"" if folder is None else _part(b'form-data; name="folder"', folder)
    return prefix + _part(b'form-data; name="file"; filename="note.txt"', data) + _end()


def _end() -> bytes:
    """終端欠落 test と同じ境界を共有する。"""

    return b"--bounded-upload--\r\n"


def _scope(headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    """ASGI receive を直接監視できる合成 request scope を作る。"""

    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": f"/api/v1/projects/{uuid4()}/documents", "query_string": b"",
        "root_path": "", "headers": [
            *(headers or [(b"content-type", _CONTENT_TYPE)]),
            (b"idempotency-key", str(uuid4()).encode()),
        ],
        "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
    }


def _request(
    body: bytes, *, chunk_size: int = 7, headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[Request, list[int]]:
    """分断された header/boundary と receive 回数を実 stream で検査する。"""

    calls: list[int] = []
    offset = 0

    async def receive() -> Message:
        """残りだけを渡し、読み終えた後の過剰 receive は test を失敗させる。"""

        nonlocal offset
        assert offset < len(body)
        chunk = body[offset:offset + chunk_size]
        offset += len(chunk)
        calls.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": offset < len(body)}

    return Request(_scope(headers), receive), calls


@pytest.mark.parametrize("denial", ["session", "origin", "csrf", "project", "archive"])
async def test_upload_authorization_rejects_before_asgi_receive(
    client: TestClient, denial: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """multipart が storage 不呼出だけでなく、ASGI 正文を一度も読まないことを証明する。"""

    app = client.app
    assert isinstance(app, FastAPI)
    auth = app.state.auth_service
    fake = FakeDocumentService()
    spy = AsyncMock(wraps=fake.upload_document)
    monkeypatch.setattr(fake, "upload_document", spy)
    app.state.document_service = fake
    origin = b"http://testserver"
    csrf = auth.csrf_token.encode()
    expected = {"session": 401, "origin": 403, "csrf": 403, "project": 404, "archive": 409}
    if denial == "session":
        auth.unauthorized = True
    elif denial == "origin":
        origin = b"https://outside.example"
    elif denial == "csrf":
        csrf = b"incorrect"
    elif denial == "project":
        app.state.project_service = DeniedProjectAuthorizationService()
    else:
        original = app.state.project_service.get_project

        async def archived(**kwargs: object) -> object:
            """同じ Project actor dependency に帰档を観測させる。"""

            return replace(await original(**kwargs), status=ProjectStatus.ARCHIVED)

        monkeypatch.setattr(app.state.project_service, "get_project", archived)
    receive = AsyncMock(side_effect=AssertionError("Authentication must precede body reads"))
    messages: list[Message] = []

    async def send(message: Message) -> None:
        """実 app response を集め、拒否 status と no-store を確認する。"""

        messages.append(message)

    await app(_scope([
        (b"content-type", _CONTENT_TYPE), (b"origin", origin), (b"x-csrf-token", csrf),
    ]), receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    assert start["status"] == expected[denial]
    assert (b"cache-control", b"no-store") in start["headers"]
    receive.assert_not_called()
    spy.assert_not_called()


@pytest.mark.parametrize("size", [1, 7, 64 * 1024, 256 * 1024])
async def test_upload_reader_handles_fragmented_binary_and_utf8(size: int) -> None:
    """分割位置に依存せず、正文を decode せず同じ byte と Unicode folder を返す。"""

    data = bytes(range(256)) * 32
    request, calls = _request(_body(data, folder="設計/資料".encode()), chunk_size=size)
    result = await read_document_upload(request, max_bytes=len(data))
    assert result.data == data and result.folder == "設計/資料" and result.name == "note.txt"
    assert result.content_type == "application/octet-stream"
    assert calls


@pytest.mark.parametrize("length", [None, b"1"])
async def test_actual_file_limit_does_not_trust_length(length: bytes | None) -> None:
    """未申告/低申告でも file の超過断片を受理せず、storage を呼ぶ前に止める。"""

    headers = [(b"content-type", _CONTENT_TYPE)]
    if length:
        headers.append((b"content-length", length))
    request, calls = _request(_body(b"x" * 100), headers=headers)
    with pytest.raises(ProblemException) as caught:
        await read_document_upload(request, max_bytes=8)
    assert caught.value.status == 413 and caught.value.code == "document_upload_too_large"
    assert sum(calls) < len(_body(b"x" * 100))


async def test_total_body_limit_counts_epilogue_after_complete_file() -> None:
    """on_end 後の byte も計数し、既に完結した小 file を先に保存しない。"""

    body = _body() + b"x" * MULTIPART_OVERHEAD_BYTES
    request, _ = _request(body, chunk_size=1024)
    with pytest.raises(ProblemException) as caught:
        await read_document_upload(request, max_bytes=4)
    assert caught.value.status == 413


@pytest.mark.parametrize("body", [
    _body()[:-len(_end())],
    _body()[:-5],
    _part(b'form-data; name="folder"', b"specs") + _end(),
    _part(b'form-data; name="file"', b"text-field") + _end(),
    _part(b'form-data; name="folder"; filename="folder.txt"', b"x") + _end(),
    _part(b'form-data; name="extra"', b"x") + _body(),
    _part(b'form-data; name="file"; filename="a.txt"', b"x") + _body(),
    _part(b'form-data; name="folder"', b"a") + _body(folder=b"b"),
    _part(b'form-data; name="folder"', b"x" * 1025) + _body(),
    _body(folder=b"\xff"),
    _part(b'form-data; name="file"; filename="\xff"', b"x") + _end(),
    _part(b'form-data; name="other"; name="file"; filename="x"', b"x") + _end(),
    _part(b'form-data; name="file"; filename="x"; filename*=UTF-8\'\'y', b"x") + _end(),
    _part(b'form-data; name="file"; filename="x"', b"x",
          b"Content-Transfer-Encoding: base64\r\n") + _end(),
    _part(b'form-data; name="file"; filename="x"', b"x",
          b"Content-Disposition: form-data; name=other\r\n") + _end(),
    _part(b'form-data; name="file"; filename="x"', b"x",
          b"Content-Type: text/plain\r\nContent-Type: text/html\r\n") + _end(),
    _part(b'form-data; name="file"; filename="x.txt', b"x") + _end(),
    _part(b'form-data; name="file"; filename="x.txt" trailing', b"x") + _end(),
    _part(b'form-data; name="file"; filename="x.txt" (comment)', b"x") + _end(),
    _part(b"form-data " + b"(" * 1200 + b")" * 1200
          + b'; name="file"; filename="x.txt"', b"x") + _end(),
    _part(b'form-data; name="file"; filename="x"', b"x",
          b"Content-Type: text/plain;\nUnexpected=header\r\n") + _end(),
    _part(b'form-data; name="file"; filename="x"', b"x",
          b"Content-Type: text/plain;\x00x\r\n") + _end(),
    b"malformed multipart",
])
async def test_invalid_multipart_is_a_static_refusal(body: bytes) -> None:
    """多重 field/曖昧な header/切断された構造を保存可能な一 file に修復しない。"""

    request, _ = _request(body)
    with pytest.raises(ProblemException) as caught:
        await read_document_upload(request, max_bytes=2000)
    assert caught.value.status == 422 and caught.value.code == "invalid_document_upload"


@pytest.mark.parametrize("headers,status", [
    ([(b"content-type", b"application/json")], 422),
    ([(b"content-type", b"multipart/form-data")], 422),
    ([(b"content-type", _CONTENT_TYPE), (b"content-type", _CONTENT_TYPE)], 422),
    ([(b"content-type", _CONTENT_TYPE + b"; boundary=other")], 422),
    ([(b"content-type", _CONTENT_TYPE + b"; charset=latin-1")], 422),
    ([(b"content-type", _CONTENT_TYPE), (b"content-encoding", b"gzip")], 422),
    ([(b"content-type", _CONTENT_TYPE), (b"content-length", b"99999999")], 413),
    ([(b"content-type", _CONTENT_TYPE), (b"content-length", b"-1")], 422),
    ([(b"content-type", _CONTENT_TYPE), (b"content-length", b"1"),
      (b"content-length", b"2")], 422),
])
async def test_bad_envelope_headers_are_rejected_without_reading(
    headers: list[tuple[bytes, bytes]], status: int,
) -> None:
    """申告だけで拒否できる場合は一時 buffer への読取を開始しない。"""

    request, calls = _request(_body(), headers=headers)
    with pytest.raises(ProblemException) as caught:
        await read_document_upload(request, max_bytes=8)
    assert caught.value.status == status and not calls


@pytest.mark.parametrize("kind", ["disconnect", "native_cancel", "scope_cancel"])
async def test_upload_cancellation_has_no_spool_or_background_writer(
    kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部分 byte の所有 buffer を解放し、取消/断連を成功や保存へ変換しない。"""

    import starlette.formparsers

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", Mock(
        side_effect=AssertionError("Document upload must not spool")
    ))
    cleared: list[bool] = []
    original = document_upload._MultipartUpload.clear

    def clear(upload: document_upload._MultipartUpload) -> None:
        """実 buffer に部分本文があったことと、finally で解放したことを記録する。"""

        assert upload.data
        original(upload)
        cleared.append(not upload.data and not upload.folder)

    monkeypatch.setattr(document_upload._MultipartUpload, "clear", clear)
    ready = asyncio.Event()
    calls = 0

    async def receive() -> Message:
        """一断片の後で disconnect または cancellable な受信待ちを発生させる。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": _body()[:-len(_end())], "more_body": True}
        ready.set()
        if kind == "disconnect":
            return {"type": "http.disconnect"}
        await asyncio.Future[None]()
        raise AssertionError("Cancelled receive unexpectedly resumed")

    request = Request(_scope(), receive)
    if kind == "disconnect":
        with pytest.raises(ClientDisconnect):
            await read_document_upload(request, max_bytes=8)
    elif kind == "native_cancel":
        task = asyncio.create_task(read_document_upload(request, max_bytes=8))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done()
    else:
        with anyio.CancelScope() as cancel_scope:
            async def cancel_ready() -> None:
                """AnyIO scope による実際の cancellation も同じ finally を通す。"""

                await ready.wait()
                cancel_scope.cancel()

            async with anyio.create_task_group() as group:
                group.start_soon(cancel_ready)
                await read_document_upload(request, max_bytes=8)
    assert cleared == [True]


async def test_buffered_asgi_chunk_has_cooperative_cancellation() -> None:
    """既にbuffer済みの大chunkでもcallbackを処理し続けず、取消を観測する。"""

    body = _body(b"x" * (1024 * 1024))
    task = asyncio.current_task()
    assert task is not None

    async def receive() -> Message:
        """一回で全bodyを返す間に取消を予約し、次のcheckpointで受け取る。"""

        asyncio.get_running_loop().call_soon(task.cancel)
        return {"type": "http.request", "body": body, "more_body": False}

    with pytest.raises(asyncio.CancelledError):
        await read_document_upload(Request(_scope(), receive), max_bytes=1024 * 1024)
    task.uncancel()


async def test_huge_single_chunk_is_refused_before_parser_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """server所有の一chunkが大きくても、parserへslice/copyする前に総量を拒否する。"""

    write = Mock(side_effect=AssertionError("Oversized chunk must not enter the parser"))
    monkeypatch.setattr(MultipartParser, "write", write)
    request, calls = _request(
        _body(b"x" * (MULTIPART_OVERHEAD_BYTES + 8)), chunk_size=1024 * 1024,
    )
    with pytest.raises(ProblemException) as caught:
        await read_document_upload(request, max_bytes=8)
    assert caught.value.status == 413 and len(calls) == 1
    write.assert_not_called()


async def test_folder_byte_boundary_is_not_silently_truncated() -> None:
    """HTTP field byte上限は受理し、文字数policyは後続serviceの責務に残す。"""

    folder = "資" * 341 + "a"
    assert len(folder.encode()) == 1024
    request, _ = _request(_body(folder=folder.encode()), chunk_size=1)
    result = await read_document_upload(request, max_bytes=4)
    assert result.folder == folder


async def test_aggregate_header_limit_prevents_metadata_growth() -> None:
    """二partに分けても同じheader合計制限を使い、上限をfileの容量へ振り替えない。"""

    body = _part(b'form-data; name="folder"', b"", b'Content-Type: text/plain; x="'
                 + b"a" * 8300 + b'"\r\n')
    body += _part(b'form-data; name="file"; filename="x"', b"x",
                  b'Content-Type: text/plain; x="' + b"a" * 8300 + b'"\r\n') + _end()
    request, _ = _request(body, chunk_size=1000)
    with pytest.raises(ProblemException) as caught:
        await read_document_upload(request, max_bytes=1000)
    assert caught.value.status == 413 and caught.value.code == "document_upload_too_large"


@pytest.mark.parametrize("chunk_size", [1, 65536])
@pytest.mark.parametrize("filename", [b"a;b.txt", b"a(b).txt", b'a\\"b.txt'])
async def test_quoted_parameter_and_case_insensitive_media_are_preserved(
    chunk_size: int, filename: bytes,
) -> None:
    """引号内のsemicolonをparameter区切りとせず、標準media typeの大小文字を受理する。"""

    request, _ = _request(
        _part(b'Form-Data; name="file"; filename="' + filename + b'"', b"body") + _end(),
        headers=[(b"content-type", b'Multipart/Form-Data; boundary="bounded-upload"')],
        chunk_size=chunk_size,
    )
    upload = await read_document_upload(request, max_bytes=4)
    assert upload.name == filename.decode().replace('\\"', '"') and upload.data == b"body"


@pytest.mark.parametrize("revoke_during_read", [False, True])
async def test_http_upload_connects_original_auth_to_real_service(
    client: TestClient, revoke_during_read: bool,
) -> None:
    """実route→原access→業務service/SQL fakeまでつなぎ、受信中撤権でPUTしない。"""

    app = client.app
    assert isinstance(app, FastAPI)
    db = UploadDatabase()
    auth = app.state.auth_service
    auth.actor = db.access.actor
    auth.session_token, auth.csrf_token = db.access.session_token, db.access.csrf_token
    app.state.document_service = db.document_service
    received: list[bool] = []
    messages: list[Message] = []

    async def receive() -> Message:
        """actor依存を通過した後、元の資格失効と本文到着を同時に観測させる。"""

        assert not received and db.transactions == 0
        received.append(True)
        if revoke_during_read:
            db.auth_sessions = []
        return {"type": "http.request", "body": _part(
            b'form-data; name="file"; filename="note.txt"', b"hello",
            b"Content-Type: text/plain\r\n",
        ) + _end(), "more_body": False}

    async def send(message: Message) -> None:
        """実appの応答を集め、保存成功と資格拒否の境界を検証する。"""

        messages.append(message)

    scope = _scope([
        (b"content-type", _CONTENT_TYPE), (b"origin", b"http://testserver"),
        (b"x-csrf-token", db.access.csrf_token.encode()),
        (b"cookie", (app.state.settings.auth_session_cookie_name + "="
                     + db.access.session_token).encode()),
    ])
    scope["path"] = f"/api/v1/projects/{db.project.id}/documents"
    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    assert start["status"] == (401 if revoke_during_read else 201)
    assert len(db.documents) == int(not revoke_during_read)
    assert db.storage.put.await_count == int(not revoke_during_read)
    assert (b"cache-control", b"no-store") in start["headers"]


@pytest.mark.parametrize("body,status,code", [
    (_body(b"x" * 9), 413, "document_upload_too_large"),
    (_body() + b"x" * MULTIPART_OVERHEAD_BYTES, 413, "document_upload_too_large"),
    (_body()[:-len(_end())], 422, "invalid_document_upload"),
    (_part(b'form-data; name="extra"', b"x") + _body(), 422, "invalid_document_upload"),
])
def test_http_admission_refusal_never_calls_upload_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, body: bytes, status: int, code: str,
) -> None:
    """実routeで接收拒否が保存呼出前に終わり、安定Problem/no-storeを返す。"""

    assert isinstance(client.app, FastAPI)
    fake = FakeDocumentService()
    fake.max_upload_bytes = 8
    spy = AsyncMock(side_effect=AssertionError("No storage for rejected body"))
    monkeypatch.setattr(fake, "upload_document", spy)
    client.app.state.document_service = fake
    response = client.post(f"/api/v1/projects/{uuid4()}/documents", content=body,
                           headers={"Content-Type": _CONTENT_TYPE.decode(),
                                    "Idempotency-Key": str(uuid4())})
    assert response.status_code == status and response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/problem+json")
    spy.assert_not_called()


@pytest.mark.parametrize("error,status,code", [
    (UnauthorizedSessionError, 401, "authentication_required"),
    (CsrfRejectedError, 403, "csrf_rejected"),
    (ProjectNotFoundError, 404, "project_not_found"),
    (ProjectArchivedError, 409, "project_archived"),
    (DocumentConflictError, 409, "document_conflict"),
])
def test_upload_maps_original_transaction_refusals(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    error: type[Exception], status: int, code: str,
) -> None:
    """入口通過後の資格失効や同名拒否も、内部正文なしの安定 Problem にする。"""

    assert isinstance(client.app, FastAPI)
    fake = FakeDocumentService()
    monkeypatch.setattr(fake, "upload_document", AsyncMock(side_effect=error("private details")))
    client.app.state.document_service = fake
    response = client.post(f"/api/v1/projects/{uuid4()}/documents",
                           headers={"Idempotency-Key": str(uuid4())},
                           files={"file": ("note.txt", b"body", "text/plain")})
    assert response.status_code == status and response.json()["code"] == code
    assert "private details" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_upload_passes_original_credentials_and_bounded_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """multipart 読取後も元の cookie/CSRF/request ID と同じ実 byte を渡す。"""

    assert isinstance(client.app, FastAPI)
    fake = FakeDocumentService()
    spy = AsyncMock(wraps=fake.upload_document)
    monkeypatch.setattr(fake, "upload_document", spy)
    client.app.state.document_service = fake
    credentials = generate_session_credentials()
    auth = client.app.state.auth_service
    auth.session_token, auth.csrf_token = credentials.session_token, credentials.csrf_token
    client.cookies.set(
        client.app.state.settings.auth_session_cookie_name, credentials.session_token
    )
    response = client.post(f"/api/v1/projects/{uuid4()}/documents",
                           data={"folder": "資料"},
                           headers={"X-CSRF-Token": credentials.csrf_token,
                                    "Idempotency-Key": str(uuid4())},
                           files={"file": ("設計.txt", b"body", "text/plain")})
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert spy.await_args is not None
    access = spy.await_args.kwargs["access"]
    assert isinstance(access, UserAccess) and isinstance(access.request_id, UUID)
    assert access.session_token == credentials.session_token
    assert access.csrf_token == credentials.csrf_token
    assert isinstance(spy.await_args.kwargs["upload_key"], UUID)
    assert str(access.request_id) == response.headers["x-request-id"]
    assert fake.uploaded == [("資料", "設計.txt", b"body")]


def test_upload_openapi_describes_manual_multipart_and_problems(client: TestClient) -> None:
    """手動読取に変えても公開 multipart 形状と拒否契約を OpenAPI から消さない。"""

    assert isinstance(client.app, FastAPI)
    operation = client.app.openapi()["paths"]["/api/v1/projects/{project_id}/documents"]["post"]
    schema = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert schema["required"] == ["file"] and schema["additionalProperties"] is False
    assert schema["properties"]["file"]["format"] == "binary"
    assert schema["properties"]["folder"]["maxLength"] == 200
    for status in (401, 403, 404, 409, 413, 422):
        problem = operation["responses"][str(status)]
        assert "application/problem+json" in problem["content"]
        assert "Cache-Control" in problem["headers"]
