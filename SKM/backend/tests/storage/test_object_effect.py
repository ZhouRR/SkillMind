"""条件 PUT の wire 契約と原 Artifact 核対を合成 HTTP transport で検証する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from skillmind.artifacts.domain import ArtifactContent, ArtifactMetadata
from skillmind.core.hashing import sha256_hex
from skillmind.storage.effect_write import (
    ObjectWriteConflictError,
    ObjectWriteUncertainError,
    build_object_write,
)
from skillmind.storage.s3_effect import S3ObjectWriteSource


def fixture(*, handler=None, content=b"# Reviewed\n", object_key="results/review/source.md"):
    """実接続・業務 ID・Secret を含まない Artifact と client を作る。"""

    source = S3ObjectWriteSource(
        endpoint="https://storage.example.test",
        bucket="fixture-library",
        namespace_id=uuid4(),
        access_key="fixture-access",
        secret_key="fixture-secret",
        transport=httpx.MockTransport(handler) if handler else None,
    )
    project_id, run_id = uuid4(), uuid4()
    artifact = ArtifactContent(
        ArtifactMetadata(
            artifact_ref="art_fixture",
            project_id=project_id,
            run_id=run_id,
            tool_call_id=uuid4(),
            evidence_ref="ev_fixture",
            path="output/review.md",
            size_bytes=len(content),
            mime_type="text/plain",
            checksum=f"sha256:{sha256_hex(content)}",
            created_at=datetime.now(UTC),
        ),
        content,
    )
    arguments = dict(
        effect_id=uuid4(),
        project_id=project_id,
        run_id=run_id,
        artifact=artifact,
        namespace=source.namespace,
        bucket="fixture-library",
        object_key=object_key,
        allowed_prefix="results/",
        content_type="text/markdown",
    )
    return source, build_object_write(**arguments), arguments


def response(command, *, version="original/version+1", body=None, headers=None):
    """同一 GET の原 metadata と字節を返す。実 S3 の条件判定は証明しない。"""

    fields = {
        "Content-Type": command.content_type,
        "ETag": '"opaque-etag"',
        **{f"x-amz-meta-{key}": value for key, value in command.origin_metadata.items()},
        **(headers or {}),
    }
    if version is not None:
        fields["x-amz-version-id"] = version
    return httpx.Response(
        200,
        headers=fields,
        stream=Stream(
            [command.content if body is None else body],
        ),
    )


class Stream(httpx.AsyncByteStream):
    """response の consume/close を実際に観測する、有界読取用の合成 stream。"""

    def __init__(self, chunks):
        """指定 chunk と consume 数だけを保持する。"""
        self.chunks, self.read_count, self.closed = chunks, 0, False

    async def __aiter__(self):
        """未要求の後続 chunk は yield しない。"""
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self):
        """超過・取消・例外でも cleanup したことを記録する。"""
        self.closed = True


@pytest.mark.parametrize(
    "key",
    [
        "/results/a",
        "results/../a",
        "results//a",
        "results/./a",
        "results/a ",
        "results2/a",
        "results\\a",
    ],
)
def test_noncanonical_or_out_of_scope_key_never_builds_a_command(key):
    """正規化や前方部分一致によって承認外の key を作らない。"""
    with pytest.raises(ValueError):
        fixture(object_key=key)


def test_original_run_artifact_and_all_identities_are_bound():
    """別 Run の成果・内容や保存先の改変は接続前に拒否する。"""
    _, command, arguments = fixture()
    with pytest.raises(ValueError, match="approved project"):
        build_object_write(**{**arguments, "run_id": uuid4()})
    for name, value in {
        "effect_id": uuid4(),
        "project_id": uuid4(),
        "run_id": uuid4(),
        "object_key": "results/other.md",
        "bucket": "other-bucket",
        "content": b"changed",
        "content_type": "application/json",
        "artifact_ref": "art_different",
    }.items():
        with pytest.raises(ValueError, match="checksum"):
            replace(command, **{name: value}).validate()
    assert "Reviewed" not in repr(command)


async def test_one_signed_conditional_put_reads_back_the_exact_returned_version():
    """PUT の条件・metadata・字節を署名し、返却版を GET してから成功を返す。"""
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "PUT":
            assert request.headers["if-none-match"] == "*"
            assert request.content == command.content
            signed = request.headers["authorization"].split("SignedHeaders=")[1].split(",")[0]
            assert "if-none-match" in signed.split(";")
            for key, value in command.origin_metadata.items():
                assert request.headers[f"x-amz-meta-{key}"] == value
                assert f"x-amz-meta-{key}" in signed.split(";")
            return httpx.Response(
                200,
                headers={
                    "ETag": '"opaque-etag"',
                    "x-amz-version-id": "original/version+1",
                },
                stream=Stream([]),
            )
        assert request.url.params["versionId"] == "original/version+1"
        assert request.url.path.endswith("/結果/source.md")
        return response(command)

    source, command, _ = fixture(handler=handler, object_key="results/結果/source.md")
    authorize = AsyncMock()
    receipt = await source.create_once(command, authorize=authorize)
    assert [request.method for request in requests] == ["PUT", "GET"]
    assert receipt.version_id == "original/version+1"
    assert receipt.request_checksum == command.request_checksum
    assert authorize.await_count == 4


@pytest.mark.parametrize("matches", [False, True])
async def test_precondition_failure_is_not_success_without_original_metadata(matches):
    """同名・同本文でも別 Effect の metadata を原成功と扱わず、二度目の PUT をしない。"""
    requests = []

    def handler(request):
        requests.append(request.method)
        if request.method == "PUT":
            return httpx.Response(412, stream=Stream([]))
        return response(
            command, headers={} if matches else {"x-amz-meta-skm-effect-id": str(uuid4())}
        )

    source, command, _ = fixture(handler=handler)
    if matches:
        assert (
            await source.create_once(command, authorize=AsyncMock())
        ).effect_id == command.effect_id
    else:
        with pytest.raises(ObjectWriteConflictError):
            await source.create_once(command, authorize=AsyncMock())
    assert requests == ["PUT", "GET"]


@pytest.mark.parametrize("status", [301, 307, 403, 409, 500, 503])
async def test_put_failure_or_redirect_is_unknown_without_resend(status):
    """redirect/拒否/競争/障害で宛先を替えたり PUT を自動再送したりしない。"""
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://elsewhere.example.test"},
            stream=Stream([b"private error"]),
        )

    source, command, _ = fixture(handler=handler)
    with pytest.raises(ObjectWriteUncertainError, match="requires reconciliation") as caught:
        await source.create_once(command, authorize=AsyncMock())
    assert len(requests) == 1
    assert "private" not in str(caught.value)


async def test_lost_put_response_is_reconciled_by_get_without_second_put():
    """遠端保存後の応答欠落を原 metadata/byte で確認し、現在値だけから成功を作らない。"""
    requests = []

    def handler(request):
        requests.append(request.method)
        if request.method == "PUT":
            raise httpx.ReadTimeout("private endpoint")
        return response(command, version=None)

    source, command, _ = fixture(handler=handler)
    with pytest.raises(ObjectWriteUncertainError):
        await source.create_once(command, authorize=AsyncMock())
    receipt = await source.lookup(command, authorize=AsyncMock())
    assert receipt is not None and receipt.version_id is None
    assert requests == ["PUT", "GET"]


@pytest.mark.parametrize(
    "code,version,missing",
    [
        ("NoSuchKey", None, True),
        ("NoSuchBucket", None, False),
        ("AccessDenied", None, False),
        ("NoSuchVersion", "fixed", True),
        ("NoSuchKey", "fixed", False),
        ("NoSuchVersion", None, False),
    ],
)
async def test_only_exact_s3_object_absence_is_none(code, version, missing):
    """404 の種類を区別し、bucket/権限/別版の不足を未書込と誤認しない。"""
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(404, stream=Stream([f"<Error><Code>{code}</Code></Error>".encode()]))

    source, command, _ = fixture(handler=handler)
    if missing:
        assert await source.lookup(command, authorize=AsyncMock(), version_id=version) is None
    else:
        with pytest.raises(ObjectWriteUncertainError):
            await source.lookup(command, authorize=AsyncMock(), version_id=version)
    assert methods == ["GET"]


async def test_byte_limit_closes_stream_and_does_not_accept_metadata_hash():
    """サイズ申告と hash が一致しても、実本文超過は切り詰め成功にしない。"""
    stream = Stream([b"x" * 65536] * 100)
    source, command, _ = fixture(
        handler=lambda request: httpx.Response(
            200,
            headers={"Content-Length": "1"},
            stream=stream,
        )
    )
    with pytest.raises(ObjectWriteUncertainError):
        await source.lookup(command, authorize=AsyncMock())
    assert stream.closed and stream.read_count == 1


@pytest.mark.parametrize("alteration", ["bytes", "etag", "version", "encoding"])
async def test_readback_mismatch_after_put_remains_unknown(alteration):
    """PUT が成功応答でも回読が不一致なら、未変更や検証成功を断定しない。"""

    def handler(request):
        if request.method == "PUT":
            return httpx.Response(
                200,
                headers={
                    "ETag": '"opaque-etag"',
                    "x-amz-version-id": "original/version+1",
                },
                stream=Stream([]),
            )
        return response(
            command,
            body=b"different" if alteration == "bytes" else None,
            version="different" if alteration == "version" else "original/version+1",
            headers=(
                {"ETag": '"changed"'}
                if alteration == "etag"
                else {"Content-Encoding": "gzip"}
                if alteration == "encoding"
                else {}
            ),
        )

    source, command, _ = fixture(handler=handler)
    with pytest.raises(ObjectWriteUncertainError):
        await source.create_once(command, authorize=AsyncMock())


async def test_revocation_before_io_and_after_put_have_different_evidence():
    """最初の拒否は I/O 前、送信後の失権は未知として原核対を要求する。"""
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, headers={"ETag": '"opaque-etag"'}, stream=Stream([]))

    source, command, _ = fixture(handler=handler)
    with pytest.raises(PermissionError):
        await source.create_once(command, authorize=AsyncMock(side_effect=PermissionError()))
    assert not methods
    with pytest.raises(ObjectWriteUncertainError):
        await source.create_once(
            command, authorize=AsyncMock(side_effect=[None, None, PermissionError()])
        )
    assert methods == ["PUT"]


async def test_cancellation_closes_request_without_retry_or_delete():
    """CancelledError を未書込や成功へ変換せず、raw coroutine を閉じる。"""
    entered = asyncio.Event()
    closed = asyncio.Event()
    methods = []

    async def handler(request):
        methods.append(request.method)
        entered.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()

    source, command, _ = fixture(handler=handler)
    task = asyncio.create_task(source.create_once(command, authorize=AsyncMock()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and methods == ["PUT"]


async def test_namespace_change_and_tampering_fail_before_authorization():
    """旧世代/別 bucket/改変内容で接続設定を解決し直さない。"""
    source, command, arguments = fixture()
    authorize = AsyncMock()
    changed = build_object_write(
        **{**arguments, "namespace": replace(source.namespace, namespace_id=uuid4())}
    )
    with pytest.raises(ValueError, match="namespace"):
        await source.create_once(changed, authorize=authorize)
    with pytest.raises(ValueError, match="checksum"):
        await source.lookup(replace(command, content=b"changed"), authorize=authorize)
    authorize.assert_not_called()


async def test_real_loopback_http_lost_response_recovers_original_bytes():
    """本物の TCP/HTTP client で失応答を確認する。合成 server は MinIO 保証の代用ではない。"""

    methods, failures = [], []
    saved = {}
    handlers = set()

    async def handle(reader, writer):
        """PUT を保持して応答前に切断し、後の GET にだけ原本文を返す。"""
        task = asyncio.current_task()
        handlers.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            first, *lines = raw.decode("ascii").strip().split("\r\n")
            method, target, _ = first.split(" ")
            headers = dict(line.lower().split(": ", 1) for line in lines)
            methods.append(method)
            assert urlsplit(target).path == "/fixture-library/results/review/source.md"
            assert headers["authorization"].startswith("aws4-hmac-sha256 ")
            if method == "PUT":
                assert headers["if-none-match"] == "*" and not saved
                saved.update(
                    headers=headers,
                    content=await reader.readexactly(int(headers["content-length"])),
                )
            else:
                assert method == "GET" and saved
                fields = {
                    key: value
                    for key, value in saved["headers"].items()
                    if key.startswith("x-amz-meta-")
                }
                fields.update(
                    {
                        "Content-Type": "text/markdown",
                        "Content-Length": str(len(saved["content"])),
                        "ETag": '"opaque-etag"',
                        "Connection": "close",
                    }
                )
                wire = (
                    "HTTP/1.1 200 OK\r\n"
                    + "".join(f"{key}: {value}\r\n" for key, value in fields.items())
                    + "\r\n"
                )
                writer.write(wire.encode("ascii") + saved["content"])
                await writer.drain()
        except Exception as error:
            # server task の失敗を pytest 本体へ返し、未回収の background error にしない。
            failures.append(error)
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        _, _, arguments = fixture()
        source = S3ObjectWriteSource(
            endpoint=endpoint,
            bucket="fixture-library",
            namespace_id=uuid4(),
            access_key="fixture-access",
            secret_key="fixture-secret",
        )
        command = build_object_write(**{**arguments, "namespace": source.namespace})
        with pytest.raises(ObjectWriteUncertainError):
            await source.create_once(command, authorize=AsyncMock())
        receipt = await source.lookup(command, authorize=AsyncMock())
        assert receipt.content_checksum == command.content_checksum
        assert methods == ["PUT", "GET"]
        assert not failures
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)


async def test_whole_operation_deadline_closes_transport_without_another_request(monkeypatch):
    """応答 stream が進まない場合も全体期限で閉じ、内部 retry を始めない。"""

    from skillmind.storage import s3_effect

    monkeypatch.setattr(s3_effect, "_TIMEOUT_SECONDS", 0.01)
    methods = []
    closed = asyncio.Event()

    async def handler(request):
        methods.append(request.method)
        try:
            await asyncio.Future()
        finally:
            closed.set()

    source, command, _ = fixture(handler=handler)
    with pytest.raises(ObjectWriteUncertainError):
        await source.create_once(command, authorize=AsyncMock())
    assert methods == ["PUT"] and closed.is_set()
