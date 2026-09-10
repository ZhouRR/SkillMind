"""Skill multipart の実 ASGI byte 境界を、source parser/保存の責務と分離して検証する。"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import anyio
import pytest
from fastapi import Request
from python_multipart import MultipartParser
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

from skillmind.api import skill_upload
from skillmind.api.problems import ProblemException
from skillmind.api.skill_upload import (
    SKILL_MULTIPART_OVERHEAD_BYTES,
    SKILL_PART_HEADER_BYTES,
    read_skill_upload,
)
from skillmind.skills.importer import HTTP_SKILL_IMPORT_LIMITS

CONTENT_TYPE = b"multipart/form-data; boundary=skill-upload-boundary"
END = b"--skill-upload-boundary--\r\n"


def part(disposition: bytes, data: bytes, extra_headers: bytes = b"") -> bytes:
    """合成 file/不正 field を同じ wire engine へ渡し、実 file は作らない。"""

    return (
        b"--skill-upload-boundary\r\nContent-Disposition: "
        + disposition
        + b"\r\n"
        + extra_headers
        + b"\r\n"
        + data
        + b"\r\n"
    )


def file_part(data: bytes = b"# Synthetic Skill\n", filename: bytes = b"SKILL.md") -> bytes:
    """原 filename を相対 path として保持した browser 型 file part を返す。"""

    return part(b'form-data; name="files"; filename="' + filename + b'"', data)


def scope(headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    """接收回数と認証順を観測できる upload endpoint の ASGI scope を作る。"""

    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/skill-imports/upload",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", CONTENT_TYPE)] if headers is None else headers,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }


def request(
    body: bytes,
    *,
    chunk_size: int = 65536,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[Request, list[int]]:
    """断片化した header/boundary を実 Request.stream へ流す。"""

    offset = 0
    calls: list[int] = []

    async def receive() -> Message:
        """受理した断片だけを数え、既に終わった要求を再読みしない。"""

        nonlocal offset
        assert offset < len(body)
        chunk = body[offset : offset + chunk_size]
        offset += len(chunk)
        calls.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": offset < len(body)}

    return Request(scope(headers), receive), calls


@pytest.mark.parametrize("chunk_size", [1, 7, 65536, 256 * 1024])
async def test_skill_upload_keeps_order_utf8_paths_binary_and_empty_files(chunk_size: int) -> None:
    """原 binary を decode せず、複数 files と空 file の順序/名前/MIME を保持する。"""

    binary = bytes(range(256)) * 4
    body = (
        file_part()
        + part(
            'form-data; name="files"; filename="資料/図.png"'.encode(),
            binary,
            b"Content-Type: image/png\r\n",
        )
        + file_part(b"", b"assets/empty.bin")
        + END
    )
    incoming, _ = request(body, chunk_size=chunk_size)
    files = await read_skill_upload(incoming)
    assert [file.path for file in files] == ["SKILL.md", "資料/図.png", "assets/empty.bin"]
    assert files[1].data == binary and files[1].content_type == "image/png"
    assert files[2].data == b"" and files[2].content_type == "application/octet-stream"
    assert all(type(file.data) is bytes for file in files)


@pytest.mark.parametrize(
    "filename",
    [
        b"../SKILL.md",
        b"/SKILL.md",
        rb"C:\folder\SKILL.md",
        b"C:/folder/SKILL.md",
        rb"\\server\share\SKILL.md",
        b"folder/../SKILL.md",
        b"folder/./SKILL.md",
    ],
)
async def test_skill_upload_never_normalizes_paths_before_shared_source_parser(
    filename: bytes,
) -> None:
    """IE6 の basename 補正や root/dotsegment 折り畳みで原不正 path を隠さない。"""

    # 引用文字列の wire escape だけを戻し、業務上の path 正規化はしない。
    wire_name = filename.replace(b"\\", b"\\\\")
    incoming, _ = request(file_part(filename=wire_name) + END)
    files = await read_skill_upload(incoming)
    assert files[0].path == filename.decode()


@pytest.mark.parametrize("count", [100, 101])
async def test_skill_upload_enforces_file_count_at_part_admission(count: int) -> None:
    """100 file は受理し、101 番目の header/本文を蓄積する前に容量拒否する。"""

    incoming, _ = request(
        b"".join(file_part(b"", f"file-{i}".encode()) for i in range(count)) + END
    )
    if count == 100:
        assert len(await read_skill_upload(incoming)) == 100
    else:
        with pytest.raises(ProblemException) as error:
            await read_skill_upload(incoming)
        assert (error.value.status, error.value.code) == (413, "skill_upload_too_large")


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("length", [None, b"1"])
async def test_skill_upload_enforces_actual_single_file_bytes(
    extra: int, length: bytes | None
) -> None:
    """Content-Length の欠落/低申告でも 1MB の境界を正確に検査し、切り詰めない。"""

    headers = [(b"content-type", CONTENT_TYPE)]
    if length is not None:
        headers.append((b"content-length", length))
    data = b"x" * (HTTP_SKILL_IMPORT_LIMITS.max_file_bytes + extra)
    incoming, _ = request(file_part(data) + END, headers=headers)
    if not extra:
        assert (await read_skill_upload(incoming))[0].data == data
    else:
        with pytest.raises(ProblemException) as error:
            await read_skill_upload(incoming)
        assert (error.value.status, error.value.code) == (413, "skill_upload_too_large")


@pytest.mark.parametrize("extra", [0, 1])
async def test_skill_upload_enforces_total_file_bytes_independently_of_envelope(extra: int) -> None:
    """五つの合法な 1MB file の後、総 file 5MB を超える一 byte も受理しない。"""

    body = b"".join(file_part(b"x" * 1_000_000, str(index).encode()) for index in range(5))
    if extra:
        body += file_part(b"x", b"last")
    body += END
    assert len(body) < 5_000_000 + SKILL_MULTIPART_OVERHEAD_BYTES
    incoming, _ = request(body)
    if not extra:
        assert sum(len(file.data) for file in await read_skill_upload(incoming)) == 5_000_000
    else:
        with pytest.raises(ProblemException) as error:
            await read_skill_upload(incoming)
        assert (error.value.status, error.value.code) == (413, "skill_upload_too_large")


async def test_skill_upload_counts_epilogue_and_refuses_huge_chunk_before_parser_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """終端後の epilogue も全体へ含め、巨大 ASGI chunk を copy する前に拒否する。"""

    body = file_part() + END + b"x" * (5_000_000 + SKILL_MULTIPART_OVERHEAD_BYTES)
    writer = Mock(side_effect=AssertionError("Oversized chunk must not be copied"))
    monkeypatch.setattr(MultipartParser, "write", writer)
    incoming, calls = request(body, chunk_size=len(body))
    with pytest.raises(ProblemException) as error:
        await read_skill_upload(incoming)
    assert error.value.status == 413 and len(calls) == 1
    writer.assert_not_called()


@pytest.mark.parametrize("chunk_size", [1, 65536, 1024 * 1024])
@pytest.mark.parametrize("kind", ["single", "aggregate"])
async def test_skill_upload_header_excess_is_413_not_parser_structure_422(
    chunk_size: int,
    kind: str,
) -> None:
    """engine 内部例外の分類に依存せず、分割位置を問わず実 header 超過を 413 にする。"""

    size, count = (SKILL_PART_HEADER_BYTES + 1, 1) if kind == "single" else (15000, 9)
    body = (
        b"".join(
            part(
                b'form-data; name="files"; filename="file"',
                b"",
                b'Content-Type: text/plain; note="' + b"x" * size + b'"\r\n',
            )
            for _ in range(count)
        )
        + END
    )
    incoming, _ = request(body, chunk_size=chunk_size)
    with pytest.raises(ProblemException) as error:
        await read_skill_upload(incoming)
    assert (error.value.status, error.value.code) == (413, "skill_upload_too_large")


@pytest.mark.parametrize(
    "body",
    [
        END,
        file_part()[:-2],
        file_part() + END[:-5],
        b"malformed multipart",
        part(b'form-data; name="files"', b"ordinary field") + END,
        part(b'form-data; name="file"; filename="SKILL.md"', b"x") + END,
        part(b'form-data; name="other"; filename="SKILL.md"', b"x") + END,
        part(b'form-data; name="files"; name="other"; filename="x"', b"x") + END,
        part(b'form-data; name="files"; filename="x"; filename*=UTF-8\'\'y', b"x") + END,
        part(b'form-data; name="files"; filename="\xff"', b"x") + END,
        part(b'form-data; name="files"; filename="unterminated', b"x") + END,
        part(b'form-data; name="files"; filename="x" trailing', b"x") + END,
        part(b'form-data; name="files"; filename="x" (comment)', b"x") + END,
        part(
            b'form-data; name="files"; filename="x"', b"x", b"Content-Transfer-Encoding: base64\r\n"
        )
        + END,
        part(
            b'form-data; name="files"; filename="x"',
            b"x",
            b"Content-Type: text/plain\r\nContent-Type: text/plain\r\n",
        )
        + END,
        part(
            b'form-data; name="files"; filename="x"',
            b"x",
            b"Content-Disposition: form-data; name=other\r\n",
        )
        + END,
    ],
)
async def test_skill_upload_rejects_ambiguous_or_partial_structure(body: bytes) -> None:
    """通常 field、未知 field、曖昧な header と未完の終端を、静的拒否へ畳み込む。"""

    incoming, _ = request(body, chunk_size=7)
    with pytest.raises(ProblemException) as error:
        await read_skill_upload(incoming)
    assert (error.value.status, error.value.code) == (422, "invalid_skill_upload")
    assert error.value.detail == skill_upload.invalid_skill_upload().detail


@pytest.mark.parametrize(
    "headers,status",
    [
        ([], 422),
        ([(b"content-type", b"application/json")], 422),
        ([(b"content-type", CONTENT_TYPE), (b"content-type", CONTENT_TYPE)], 422),
        ([(b"content-type", CONTENT_TYPE + b"; boundary=other")], 422),
        ([(b"content-type", CONTENT_TYPE + b"; charset=latin-1")], 422),
        ([(b"content-type", CONTENT_TYPE), (b"content-encoding", b"gzip")], 422),
        ([(b"content-type", CONTENT_TYPE), (b"content-length", b"-1")], 422),
        (
            [(b"content-type", CONTENT_TYPE), (b"content-length", b"1"), (b"content-length", b"1")],
            422,
        ),
        ([(b"content-type", CONTENT_TYPE), (b"content-length", b"5131073")], 413),
        ([(b"content-type", CONTENT_TYPE + b"x" * SKILL_PART_HEADER_BYTES)], 413),
    ],
)
async def test_skill_upload_rejects_bad_envelope_without_receiving_body(
    headers: list[tuple[bytes, bytes]],
    status: int,
) -> None:
    """header だけで拒否できる要求は ASGI receive を一度も呼ばない。"""

    incoming, calls = request(file_part() + END, headers=headers)
    with pytest.raises(ProblemException) as error:
        await read_skill_upload(incoming)
    assert error.value.status == status
    assert calls == []


@pytest.mark.parametrize("kind", ["disconnect", "native-cancel", "scope-cancel"])
async def test_skill_upload_releases_partial_files_without_spool_or_background_writer(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既読 file と部分 file の両 buffer を破棄し、取消/断連を保存や成功へ変換しない。"""

    import starlette.formparsers

    monkeypatch.setattr(
        starlette.formparsers,
        "SpooledTemporaryFile",
        Mock(
            side_effect=AssertionError("Skill upload must not spool"),
        ),
    )
    cleared: list[bool] = []
    original = skill_upload._SkillUpload.clear

    def clear(receiver: skill_upload._SkillUpload) -> None:
        """実際に部分 byte があった上で、finally が保持参照を外すことを確認する。"""

        assert receiver.files and receiver.data
        original(receiver)
        cleared.append(not receiver.files and not receiver.data)

    monkeypatch.setattr(skill_upload._SkillUpload, "clear", clear)
    ready = asyncio.Event()
    calls = 0

    async def receive() -> Message:
        """一 file 完了と次 file 途中の後、取消可能な受信待機へ進む。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "type": "http.request",
                "body": file_part() + file_part(b"partial"),
                "more_body": True,
            }
        ready.set()
        if kind == "disconnect":
            return {"type": "http.disconnect"}
        await asyncio.Future[None]()
        raise AssertionError("Cancelled receive unexpectedly resumed")

    incoming = Request(scope(), receive)
    if kind == "disconnect":
        with pytest.raises(ClientDisconnect):
            await read_skill_upload(incoming)
    elif kind == "native-cancel":
        task = asyncio.create_task(read_skill_upload(incoming))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with anyio.CancelScope() as cancel_scope:

            async def cancel_ready() -> None:
                """AnyIO cancellation も native cancellation と同じ解放経路を通す。"""

                await ready.wait()
                cancel_scope.cancel()

            async with anyio.create_task_group() as group:
                group.start_soon(cancel_ready)
                await read_skill_upload(incoming)
    assert cleared == [True]
