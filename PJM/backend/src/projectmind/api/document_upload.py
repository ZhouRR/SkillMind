"""認証後だけ multipart を有界で受け取り、文書 service 用の原 byte を返す。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message
from email.policy import HTTP
from typing import TYPE_CHECKING

from anyio.lowlevel import checkpoint
from fastapi import Request
from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from projectmind.api.problems import ProblemException

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

MULTIPART_OVERHEAD_BYTES = 16 * 1024
_FIELD_BYTES = 1024
_CHUNK_BYTES = 64 * 1024
_BOUNDARY = re.compile(rb"[0-9A-Za-z'()+_,./:=? -]{1,70}")


@dataclass(frozen=True, slots=True)
class DocumentUpload:
    """HTTP の構造を検証済みの一文書。名前・MIME・内容 policy は service が判定する。"""

    folder: str
    name: str
    data: bytes
    content_type: str


def _invalid_upload() -> ProblemException:
    """parser の入力値や内部例外を公開しない固定の構造拒否を返す。"""

    return ProblemException(
        status=422, title="Invalid document upload",
        detail="Expected one file and at most one UTF-8 folder in a complete multipart body.",
        code="invalid_document_upload",
    )


def _upload_too_large() -> ProblemException:
    """file と envelope の実 byte 超過を、書込前の明確な拒否にする。"""

    return ProblemException(
        status=413, title="Document upload too large",
        detail="The document upload exceeds the file or multipart size limit.",
        code="document_upload_too_large",
    )


def _header_options(
    value: bytes | str, *, header: str = "content-type",
) -> tuple[bytes, dict[bytes, bytes]]:
    """重複/拡張 parameter を先に拒否し、共有 parser の last-wins を許可にしない。"""

    raw = value.decode("latin-1") if isinstance(value, bytes) else value
    if any(character in raw for character in "\r\n\x00"):
        raise _invalid_upload()
    quoted = escaped = False
    for character in raw:
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif not quoted and character in "()":
            # MIME comment の解釈は parser 間で異なる。引用内の通常の file 名は維持する。
            raise _invalid_upload()
    try:
        if HTTP.header_factory(header, raw).defects:
            raise _invalid_upload()
    except RecursionError as error:
        # byte 上限内でも深い comment が標準 parser の再帰限界へ届くため、構造拒否に閉じる。
        raise _invalid_upload() from error
    message = Message()
    message["content-type"] = raw
    parameters = message.get_params(header="content-type", unquote=False)
    if not parameters:
        raise _invalid_upload()
    seen: set[str] = set()
    for name, parameter in parameters[1:]:
        if name.lower() in seen or isinstance(parameter, tuple) or "*" in name:
            raise _invalid_upload()
        seen.add(name.lower())
    media, options = parse_options_header(value)
    return media.lower(), options


def _boundary(request: Request, max_body_bytes: int) -> bytes:
    """長さ申告は早期拒否にだけ使い、曖昧な header や圧縮 envelope を受理しない。"""

    headers = request.headers
    content_types = headers.getlist("content-type")
    if len(content_types) != 1 or len(content_types[0]) > MULTIPART_OVERHEAD_BYTES:
        raise _invalid_upload()
    encodings = headers.getlist("content-encoding")
    if len(encodings) > 1 or (encodings and encodings[0].lower() != "identity"):
        raise _invalid_upload()
    lengths = headers.getlist("content-length")
    if len(lengths) > 1:
        raise _invalid_upload()
    if lengths:
        value = lengths[0]
        if not value or not value.isascii() or not value.isdecimal() or len(value) > 20:
            raise _invalid_upload()
        if int(value) > max_body_bytes:
            raise _upload_too_large()
    mime, options = _header_options(content_types[0])
    boundary = options.get(b"boundary", b"")
    if (
        mime != b"multipart/form-data"
        or _BOUNDARY.fullmatch(boundary) is None
        or boundary.endswith(b" ")
        or options.get(b"charset", b"utf-8").lower() not in {b"utf-8", b"utf8"}
    ):
        raise _invalid_upload()
    return boundary


async def read_document_upload(request: Request, *, max_bytes: int) -> DocumentUpload:
    """実 ASGI byte と一 file を制限し、拒否/取消でも一時 file や writer を残さない。"""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("A positive document upload limit is required")
    max_body_bytes = max_bytes + MULTIPART_OVERHEAD_BYTES
    upload = _MultipartUpload(max_bytes)
    parser = MultipartParser(
        _boundary(request, max_body_bytes), upload.callbacks(),
        max_header_count=2, max_header_size=MULTIPART_OVERHEAD_BYTES,
    )
    received = 0
    try:
        # service は原 byte 全体を必要とする。spool と thread を作らず一要求の buffer を制限し、
        # native Task.cancel と disk writer の競合を避ける。全要求共通の同時実行予算ではない。
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_body_bytes:
                raise _upload_too_large()
            for offset in range(0, len(chunk), _CHUNK_BYTES):
                parser.write(chunk[offset:offset + _CHUNK_BYTES])
                # ASGI が本文全体を既に持っていても、取消と他要求に実行機会を返す。
                await checkpoint()
        parser.finalize()
        return upload.finish()
    except (MultipartParseError, UnicodeError) as error:
        raise _invalid_upload() from error
    finally:
        # 取消/断連はそのまま伝播する。追跡可能な byte buffer のみを同期的に解放する。
        upload.clear()


class _MultipartUpload:
    """既存 multipart engine の callback に文書 endpoint の固定 field 契約を与える。"""

    def __init__(self, max_bytes: int) -> None:
        """一 file、任意一 folder と小さい header の所有 buffer を初期化する。"""

        self.max_bytes = max_bytes
        self.data = bytearray()
        self.folder = bytearray()
        self.filename = ""
        self.content_type = "application/octet-stream"
        self.complete = False
        self.seen: set[bytes] = set()
        self.current = b""
        self.headers: dict[bytes, bytes] = {}
        self.header_name = bytearray()
        self.header_value = bytearray()
        self.header_bytes = 0

    def callbacks(self) -> MultipartCallbacks:
        """wire 構文はライブラリに任せ、業務公開 field だけを検査する。"""

        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_end": self.on_end,
        }

    def on_part_begin(self) -> None:
        """三つ目の part は名前や本文を蓄積する前に拒否する。"""

        if len(self.seen) >= 2:
            raise _invalid_upload()
        self.headers.clear()
        self.current = b""

    def _header_size(self, size: int) -> None:
        """断片化した header も合計上限を使い、巨大な metadata を蓄積しない。"""

        self.header_bytes += size
        if self.header_bytes > MULTIPART_OVERHEAD_BYTES:
            raise _upload_too_large()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        """分割された header 名を小さい共有上限の範囲で保持する。"""

        self._header_size(end - start)
        self.header_name.extend(data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        """分割された header 値を小さい共有上限の範囲で保持する。"""

        self._header_size(end - start)
        self.header_value.extend(data[start:end])

    def on_header_end(self) -> None:
        """重複や変換 encoding を許さず、file の解釈を一意にする。"""

        name = bytes(self.header_name).lower()
        if name not in {b"content-disposition", b"content-type"} or name in self.headers:
            raise _invalid_upload()
        value = bytes(self.header_value)
        _header_options(value, header=name.decode("ascii"))
        self.headers[name] = value
        self.header_name.clear()
        self.header_value.clear()

    def on_headers_finished(self) -> None:
        """file は filename 付き、folder は通常 field として一回だけ受け取る。"""

        disposition, options = _header_options(
            self.headers.get(b"content-disposition", b""), header="content-disposition"
        )
        name = options.get(b"name", b"")
        if (
            disposition != b"form-data"
            or name not in {b"file", b"folder"}
            or name in self.seen
            or (b"filename" in options) != (name == b"file")
        ):
            raise _invalid_upload()
        self.seen.add(name)
        self.current = name
        if name == b"file":
            self.filename = options[b"filename"].decode("utf-8")
            self.content_type = self.headers.get(
                b"content-type", b"application/octet-stream"
            ).decode("latin-1")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        """上限判定を append 前に行い、超過した file 断片を保持しない。"""

        if self.current == b"file":
            if len(self.data) + end - start > self.max_bytes:
                raise _upload_too_large()
            self.data.extend(data[start:end])
        elif self.current == b"folder":
            if len(self.folder) + end - start > _FIELD_BYTES:
                raise _invalid_upload()
            self.folder.extend(data[start:end])
        else:
            raise _invalid_upload()

    def on_end(self) -> None:
        """finalize 自体は終端を保証しないため、実際の closing boundary を記録する。"""

        self.complete = True

    def finish(self) -> DocumentUpload:
        """完結した一 file だけを immutable byte にして業務境界へ渡す。"""

        if not self.complete or b"file" not in self.seen:
            raise _invalid_upload()
        return DocumentUpload(
            folder=self.folder.decode("utf-8"), name=self.filename,
            data=bytes(self.data), content_type=self.content_type,
        )

    def clear(self) -> None:
        """例外の traceback が残っても本文を callback buffer に保持しない。"""

        self.data.clear()
        self.folder.clear()
        self.headers.clear()
        self.header_name.clear()
        self.header_value.clear()
