"""取得した object の事実を本文と結び付け、保存時刻や version を推測しない。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from skillmind.storage.blob import FileStorageError


@dataclass(frozen=True, slots=True)
class BlobObservation:
    """同一取得で照合済みの metadata。ETag は hash と解釈しない opaque 値。"""

    last_modified: datetime
    etag: str
    version_id: str | None
    size: int
    content_type: str

    def __post_init__(self) -> None:
        """欠落・不正値を既定値で補わず、時刻は UTC の aware 値に限定する。"""

        if (
            not isinstance(self.last_modified, datetime)
            or self.last_modified.tzinfo is None
            or self.last_modified.utcoffset() != UTC.utcoffset(None)
            or type(self.size) is not int
            or self.size < 0
            or not _opaque(self.etag, 1024)
            or '"' in self.etag
            or (self.version_id is not None and not _opaque(self.version_id, 1024))
            or not _opaque(self.content_type, 256)
        ):
            raise FileStorageError("Blob observation is invalid")

    @property
    def version_pinned(self) -> bool:
        """S3 の null version は上書き可能なので固定 version と扱わない。"""

        return self.version_id is not None and self.version_id != "null"

    def to_json(self) -> dict[str, object]:
        """接続・bucket・内部 key を含めず、取得時点の事実だけを Evidence に渡す。"""

        return {
            "last_modified": self.last_modified.isoformat(),
            "etag": self.etag,
            "version_id": self.version_id,
            "size": self.size,
            "content_type": self.content_type,
            "consistency": "version_pinned" if self.version_pinned else "metadata_checked",
        }


@dataclass(frozen=True, slots=True)
class ObservedBlob:
    """同一応答の本文と照合済み metadata を切り離さずに返す。"""

    data: bytes = field(repr=False)
    observation: BlobObservation


@runtime_checkable
class ObservedFileStorage(Protocol):
    """取得 metadata を検証できる backend だけが実装する任意の読取 port。"""

    async def get_observed(
        self, key: str, *, max_bytes: int, expected: BlobObservation | None = None
    ) -> ObservedBlob:
        """有界本文と原 version/metadata を返す。失敗時は通常 GET に降格しない。"""

        ...


@runtime_checkable
class InspectableFileStorage(ObservedFileStorage, Protocol):
    """本文を取得せず観測し、その観測に固定して後から取得できる backend。"""

    async def inspect(self, key: str) -> BlobObservation:
        """実 storage metadata を返す。本文 hash の検証済みとは扱わない。"""

        ...


def _opaque(value: object, limit: int) -> bool:
    """HTTP metadata を有界・非空に限定し、制御文字を Evidence へ運ばない。"""

    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value.isascii()
        and all(32 <= ord(char) < 127 for char in value)
    )
