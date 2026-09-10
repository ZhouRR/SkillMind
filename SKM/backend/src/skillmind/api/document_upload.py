"""認証後だけ multipart を有界で受け取り、文書 service 用の原 byte を返す。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from skillmind.api.multipart import (
    MultipartLimits,
    MultipartReceiver,
    header_options,
    read_multipart,
)
from skillmind.api.problems import ProblemException

MULTIPART_OVERHEAD_BYTES = 16 * 1024
_FIELD_BYTES = 1024


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
        status=422,
        title="Invalid document upload",
        detail="Expected one file and at most one UTF-8 folder in a complete multipart body.",
        code="invalid_document_upload",
    )


def _upload_too_large() -> ProblemException:
    """file と envelope の実 byte 超過を、書込前の明確な拒否にする。"""

    return ProblemException(
        status=413,
        title="Document upload too large",
        detail="The document upload exceeds the file or multipart size limit.",
        code="document_upload_too_large",
    )


async def read_document_upload(request: Request, *, max_bytes: int) -> DocumentUpload:
    """実 ASGI byte と一 file を制限し、拒否/取消でも一時 file や writer を残さない。"""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("A positive document upload limit is required")
    return await read_multipart(request, _MultipartUpload(max_bytes))


class _MultipartUpload(MultipartReceiver[DocumentUpload]):
    """既存 multipart engine の callback に文書 endpoint の固定 field 契約を与える。"""

    def __init__(self, max_bytes: int) -> None:
        """一 file、任意一 folder と小さい header の所有 buffer を初期化する。"""

        super().__init__(
            MultipartLimits(
                max_body_bytes=max_bytes + MULTIPART_OVERHEAD_BYTES,
                max_header_bytes=MULTIPART_OVERHEAD_BYTES,
                max_total_header_bytes=MULTIPART_OVERHEAD_BYTES,
                invalid=_invalid_upload,
                too_large=_upload_too_large,
                oversized_content_type=_invalid_upload,
            )
        )
        self.max_bytes = max_bytes
        self.data = bytearray()
        self.folder = bytearray()
        self.filename = ""
        self.content_type = "application/octet-stream"
        self.seen: set[bytes] = set()
        self.current = b""

    def begin_part(self) -> None:
        """三つ目の part は名前や本文を蓄積する前に拒否する。"""

        if len(self.seen) >= 2:
            raise _invalid_upload()
        self.current = b""

    def on_headers_finished(self) -> None:
        """file は filename 付き、folder は通常 field として一回だけ受け取る。"""

        disposition, options = header_options(
            self.headers.get(b"content-disposition", b""),
            header="content-disposition",
            invalid=_invalid_upload,
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

    def finish(self) -> DocumentUpload:
        """完結した一 file だけを immutable byte にして業務境界へ渡す。"""

        if not self.complete or b"file" not in self.seen:
            raise _invalid_upload()
        return DocumentUpload(
            folder=self.folder.decode("utf-8"),
            name=self.filename,
            data=bytes(self.data),
            content_type=self.content_type,
        )

    def clear(self) -> None:
        """例外の traceback が残っても本文を callback buffer に保持しない。"""

        self.data.clear()
        self.folder.clear()
        super().clear()
