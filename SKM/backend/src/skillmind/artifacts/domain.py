"""可変 workspace から独立した UTF-8 Artifact の保存・読取値を定義する。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from skillmind.core.hashing import sha256_hex

MAX_ARTIFACT_BYTES = 1_048_576
MAX_RUN_ARTIFACTS = 100
MAX_RUN_ARTIFACT_BYTES = 10_485_760
_ARTIFACT_REF = re.compile(r"art_[a-zA-Z0-9_-]+")
_EVIDENCE_REF = re.compile(r"ev_[a-zA-Z0-9_-]+")
_CHECKSUM = re.compile(r"sha256:[0-9a-f]{64}")


class ArtifactIntegrityError(ValueError):
    """保存済み Artifact の形・帰属・実 byte を確認できない静的な失敗。"""


def _invalid() -> ArtifactIntegrityError:
    """保存値や正文を error message に混入させない。"""

    return ArtifactIntegrityError("Artifact does not match its saved identity or content")


def _validate_path(path: str) -> None:
    """表示用 path も output 配下の正規相対 file に固定し、実 filesystem は参照しない。"""

    if (
        not isinstance(path, str) or not 1 <= len(path) <= 4096
        or not path.startswith("output/") or "\\" in path or not path.isprintable()
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise _invalid()
    try:
        path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise _invalid() from error


def _validate_content(content: bytes) -> None:
    """Private byte は immutable な bytes と UTF-8 の局部上限を必須にする。"""

    if type(content) is not bytes or len(content) > MAX_ARTIFACT_BYTES:
        raise _invalid()
    try:
        content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise _invalid() from error


@dataclass(frozen=True, slots=True)
class ArtifactDraft:
    """Provider が書込時に保持した原 byte。repr に本文を出さず、後から file を開かない。"""

    path: str
    content: bytes = field(repr=False)
    mime_type: str = "text/plain"

    def __post_init__(self) -> None:
        """保存前から path・実 byte・現行 producer の MIME を同じ規則で検証する。"""

        _validate_path(self.path)
        _validate_content(self.content)
        if self.mime_type != "text/plain":
            raise _invalid()

    @property
    def checksum(self) -> str:
        """不変 byte の checksum を共有 SHA-256 実装で導出する。"""

        return f"sha256:{sha256_hex(self.content)}"


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """公開を許可した帰属と保存時情報のみ。private byte や arbitrary metadata は含めない。"""

    artifact_ref: str
    project_id: UUID
    run_id: UUID
    tool_call_id: UUID
    evidence_ref: str
    path: str
    size_bytes: int
    mime_type: str
    checksum: str
    created_at: datetime

    def __post_init__(self) -> None:
        """旧値を補正せず、壊れた identity・size・時刻を成功の metadata にしない。"""

        _validate_path(self.path)
        if (
            not isinstance(self.artifact_ref, str) or len(self.artifact_ref) > 64
            or _ARTIFACT_REF.fullmatch(self.artifact_ref) is None
            or not isinstance(self.evidence_ref, str) or len(self.evidence_ref) > 64
            or _EVIDENCE_REF.fullmatch(self.evidence_ref) is None
            or any(not isinstance(value, UUID) or value.int == 0 for value in (
                self.project_id, self.run_id, self.tool_call_id,
            ))
            or type(self.size_bytes) is not int or not 0 <= self.size_bytes <= MAX_ARTIFACT_BYTES
            or self.mime_type != "text/plain"
            or not isinstance(self.checksum, str) or _CHECKSUM.fullmatch(self.checksum) is None
            or not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise _invalid()


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    """同一 SELECT で得た保存情報と検証済み byte を返す。正文は repr に含めない。"""

    metadata: ArtifactMetadata
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        """実 byte を size/hash/UTF-8 と照合し、保存 checksum の書換えで修復しない。"""

        if not isinstance(self.metadata, ArtifactMetadata):
            raise _invalid()
        _validate_content(self.content)
        if (
            len(self.content) != self.metadata.size_bytes
            or f"sha256:{sha256_hex(self.content)}" != self.metadata.checksum
        ):
            raise _invalid()
