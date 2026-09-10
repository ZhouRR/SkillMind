"""Skill の相対 filename と原 binary を、有界 multipart から業務 parser へ渡す。"""

from __future__ import annotations

from fastapi import Request

from skillmind.api.multipart import (
    MultipartLimits,
    MultipartReceiver,
    header_options,
    read_multipart,
)
from skillmind.api.problems import ProblemException
from skillmind.skills.domain import UploadSkillFile
from skillmind.skills.importer import HTTP_SKILL_IMPORT_LIMITS, SkillImportLimits

SKILL_MULTIPART_OVERHEAD_BYTES = 128 * 1024
SKILL_PART_HEADER_BYTES = 16 * 1024


def invalid_skill_upload() -> ProblemException:
    """個別 header や source 内容を公開しない固定の構造拒否を返す。"""

    return ProblemException(
        status=422,
        title="Invalid skill upload",
        detail="Expected only files parts with UTF-8 filenames in a complete multipart body.",
        code="invalid_skill_upload",
    )


def skill_upload_too_large() -> ProblemException:
    """実 byte、file 数と header 超過を保存前の明確な容量拒否にする。"""

    return ProblemException(
        status=413,
        title="Skill upload too large",
        detail="The skill upload exceeds the file count, file, header or multipart size limit.",
        code="skill_upload_too_large",
    )


async def read_skill_upload(request: Request) -> tuple[UploadSkillFile, ...]:
    """ADMIN の入口認証後だけ読む。パス規則と source 内容は共有 Skill parser が判定する。"""

    return await read_multipart(request, _SkillUpload(HTTP_SKILL_IMPORT_LIMITS))


class _SkillUpload(MultipartReceiver[tuple[UploadSkillFile, ...]]):
    """files という file part だけを受け付け、本文と metadata を一要求の上限に閉じる。"""

    def __init__(self, limits: SkillImportLimits) -> None:
        """単 file、全 file、envelope と header の独立した上限を用意する。"""

        super().__init__(
            MultipartLimits(
                max_body_bytes=limits.max_total_bytes + SKILL_MULTIPART_OVERHEAD_BYTES,
                max_header_bytes=SKILL_PART_HEADER_BYTES,
                max_total_header_bytes=SKILL_MULTIPART_OVERHEAD_BYTES,
                invalid=invalid_skill_upload,
                too_large=skill_upload_too_large,
                oversized_content_type=skill_upload_too_large,
            )
        )
        self.file_limits = limits
        self.files: list[UploadSkillFile] = []
        self.data = bytearray()
        self.total_bytes = 0
        self.current = False
        self.filename = ""
        self.content_type = "application/octet-stream"

    def begin_part(self) -> None:
        """過剰な part の header や本文を蓄積する前に file 数を拒否する。"""

        if len(self.files) >= self.file_limits.max_files:
            raise skill_upload_too_large()
        self.current = False

    def on_headers_finished(self) -> None:
        """元 filename を補正せず渡し、通常 field や未知 field を file に変換しない。"""

        disposition, options = header_options(
            self.headers.get(b"content-disposition", b""),
            invalid=invalid_skill_upload,
            header="content-disposition",
            preserve_filename=True,
        )
        if (
            disposition != b"form-data"
            or options.get(b"name") != b"files"
            or b"filename" not in options
        ):
            raise invalid_skill_upload()
        self.filename = options[b"filename"].decode("utf-8")
        self.content_type = self.headers.get(b"content-type", b"application/octet-stream").decode(
            "latin-1"
        )
        self.current = True

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        """file 内/全体の容量を append 前に確認し、超過した断片を保持しない。"""

        if not self.current:
            raise invalid_skill_upload()
        size = end - start
        if (
            len(self.data) + size > self.file_limits.max_file_bytes
            or self.total_bytes + size > self.file_limits.max_total_bytes
        ):
            raise skill_upload_too_large()
        self.data.extend(data[start:end])
        self.total_bytes += size

    def on_part_end(self) -> None:
        """一つの原 file を凍結し、次 part と可変 buffer を共有しない。"""

        if not self.current:
            raise invalid_skill_upload()
        self.files.append(
            UploadSkillFile(
                path=self.filename,
                data=bytes(self.data),
                content_type=self.content_type,
            )
        )
        self.data.clear()
        self.current = False

    def finish(self) -> tuple[UploadSkillFile, ...]:
        """closing boundary と最低一 file が確認できた場合だけ、全原 byte を渡す。"""

        if not self.complete or not self.files or self.current:
            raise invalid_skill_upload()
        return tuple(self.files)

    def clear(self) -> None:
        """取消/断連/構造拒否の traceback に部分 source の所有 buffer を残さない。"""

        self.data.clear()
        self.files.clear()
        self.filename = ""
        self.content_type = ""
        super().clear()
