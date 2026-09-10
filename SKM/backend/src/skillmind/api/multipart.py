"""認証後の multipart 接收に共通する構文・header・ASGI byte 境界を実装する。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from email.policy import HTTP
from typing import TYPE_CHECKING

from anyio.lowlevel import checkpoint
from fastapi import Request
from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from skillmind.api.problems import ProblemException

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

_CHUNK_BYTES = 64 * 1024
_BOUNDARY = re.compile(rb"[0-9A-Za-z'()+_,./:=? -]{1,70}")


@dataclass(frozen=True, slots=True)
class MultipartLimits:
    """endpoint 固有の容量と公開 Problem を、共有構文処理から分離する。"""

    max_body_bytes: int
    max_header_bytes: int
    max_total_header_bytes: int
    invalid: Callable[[], ProblemException]
    too_large: Callable[[], ProblemException]
    oversized_content_type: Callable[[], ProblemException]


def header_options(
    value: bytes | str,
    *,
    invalid: Callable[[], ProblemException],
    header: str = "content-type",
    preserve_filename: bool = False,
) -> tuple[bytes, dict[bytes, bytes]]:
    """重複/拡張 parameter を拒否し、parser の last-wins や暗黙の path 修復を防ぐ。"""

    raw = value.decode("latin-1") if isinstance(value, bytes) else value
    if any(character in raw for character in "\r\n\x00"):
        raise invalid()
    quoted = escaped = False
    for character in raw:
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif not quoted and character in "()":
            # MIME comment は parser 間で解釈が異なる。引用内の通常の file 名は維持する。
            raise invalid()
    try:
        if HTTP.header_factory(header, raw).defects:
            raise invalid()
    except RecursionError as error:
        raise invalid() from error
    message = Message()
    message["content-type"] = raw
    parameters = message.get_params(header="content-type", unquote=False)
    if not parameters:
        raise invalid()
    seen: set[str] = set()
    for name, parameter in parameters[1:]:
        if name.lower() in seen or isinstance(parameter, tuple) or "*" in name:
            raise invalid()
        seen.add(name.lower())
    media, options = parse_options_header(value)
    if preserve_filename and b"filename" in options:
        # multipart library の IE6 basename 補正は Skill の相対 path を隠すため使わない。
        filename = message.get_param("filename", header="content-type")
        if not isinstance(filename, str):
            raise invalid()
        options[b"filename"] = filename.encode("latin-1")
    return media.lower(), options


def _boundary(request: Request, limits: MultipartLimits) -> bytes:
    """申告長は早期拒否にだけ使い、曖昧な header と圧縮 envelope を受理しない。"""

    headers = request.headers
    content_types = headers.getlist("content-type")
    if len(content_types) != 1:
        raise limits.invalid()
    if len(content_types[0]) > limits.max_header_bytes:
        raise limits.oversized_content_type()
    encodings = headers.getlist("content-encoding")
    if len(encodings) > 1 or (encodings and encodings[0].lower() != "identity"):
        raise limits.invalid()
    lengths = headers.getlist("content-length")
    if len(lengths) > 1:
        raise limits.invalid()
    if lengths:
        value = lengths[0]
        if not value or not value.isascii() or not value.isdecimal() or len(value) > 20:
            raise limits.invalid()
        if int(value) > limits.max_body_bytes:
            raise limits.too_large()
    mime, options = header_options(content_types[0], invalid=limits.invalid)
    boundary = options.get(b"boundary", b"")
    if (
        mime != b"multipart/form-data"
        or _BOUNDARY.fullmatch(boundary) is None
        or boundary.endswith(b" ")
        or options.get(b"charset", b"utf-8").lower() not in {b"utf-8", b"utf8"}
    ):
        raise limits.invalid()
    return boundary


async def read_multipart[Result](request: Request, receiver: MultipartReceiver[Result]) -> Result:
    """一要求の byte を有界で解析し、spool/thread を作らず取消・断連を伝播する。"""

    limits = receiver.limits
    try:
        parser = MultipartParser(
            _boundary(request, limits),
            receiver.callbacks(),
            max_header_count=2,
            max_header_size=limits.max_total_header_bytes,
        )
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > limits.max_body_bytes:
                raise limits.too_large()
            for offset in range(0, len(chunk), _CHUNK_BYTES):
                parser.write(chunk[offset : offset + _CHUNK_BYTES])
                # ASGI 所有の大 chunk も協調的に処理し、取消と他要求へ実行機会を返す。
                await checkpoint()
        parser.finalize()
        return receiver.finish()
    except (MultipartParseError, UnicodeError) as error:
        raise limits.invalid() from error
    finally:
        receiver.clear()


class MultipartReceiver[Result](ABC):
    """共通 header callback を所有し、許可 field と本文容量は endpoint adapter に任せる。"""

    def __init__(self, limits: MultipartLimits) -> None:
        """header 合計は part ごとにリセットせず、断片化による制限迂回を防ぐ。"""

        self.limits = limits
        self.headers: dict[bytes, bytes] = {}
        self.header_name = bytearray()
        self.header_value = bytearray()
        self.header_bytes = 0
        self.part_header_bytes = 0
        self.complete = False

    def callbacks(self) -> MultipartCallbacks:
        """wire 構文は既存 engine に委ね、同じ header/envelope 門禁を全 adapter で共有する。"""

        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_end": self.on_end,
        }

    def on_part_begin(self) -> None:
        """前 part の header を残さず、field 数の判定を本文蓄積より先に行う。"""

        self.headers.clear()
        self.part_header_bytes = 0
        self.begin_part()

    def _header_size(self, size: int) -> None:
        """分割された header を append 前に単 part と全体の上限で検査する。"""

        self.header_bytes += size
        self.part_header_bytes += size
        if (
            self.header_bytes > self.limits.max_total_header_bytes
            or self.part_header_bytes > self.limits.max_header_bytes
        ):
            raise self.limits.too_large()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        """header 名の断片を検証済み容量だけ保持する。"""

        self._header_size(end - start)
        self.header_name.extend(data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        """header 値の断片を検証済み容量だけ保持する。"""

        self._header_size(end - start)
        self.header_value.extend(data[start:end])

    def on_header_end(self) -> None:
        """重複と転送 encoding を拒否し、各 part の解釈を一意にする。"""

        name = bytes(self.header_name).lower()
        if name not in {b"content-disposition", b"content-type"} or name in self.headers:
            raise self.limits.invalid()
        value = bytes(self.header_value)
        header_options(value, invalid=self.limits.invalid, header=name.decode("ascii"))
        self.headers[name] = value
        self.header_name.clear()
        self.header_value.clear()

    def on_end(self) -> None:
        """finalize 自体は保証しない closing boundary の実到着を記録する。"""

        self.complete = True

    def on_part_end(self) -> None:
        """複数 file adapter はここで原 byte を凍結し、単 file は保持を継続できる。"""

        return

    @abstractmethod
    def begin_part(self) -> None:
        """endpoint 固有の field 数と現在 part の状態を検証する。"""

    @abstractmethod
    def on_headers_finished(self) -> None:
        """完全な header を endpoint の file/field 契約へ対応させる。"""

    @abstractmethod
    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        """endpoint の容量を超えない byte だけを保持する。"""

    @abstractmethod
    def finish(self) -> Result:
        """閉じた本文を immutable な業務入力へ変換する。"""

    def clear(self) -> None:
        """例外 traceback が残っても header buffer を保持しない。"""

        self.headers.clear()
        self.header_name.clear()
        self.header_value.clear()
