"""Blob 存储の SDK 非依存な契約、metadata、object key 安全化を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

_KEY_MAX = 512
_SEGMENT_MAX = 128


class FileStorageError(RuntimeError):
    """Blob 存储操作の基底エラー。"""


class BlobNotFoundError(FileStorageError):
    """指定 key の blob が存在しないことを表す。"""


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """保存済み blob の識別子と metadata。正文そのものは保持しない。"""

    key: str
    size: int
    content_type: str
    sha256: str


class FileStorage(Protocol):
    """Project scoped blob の非同期 put/get/delete/stat 契約。実装は差し替え可能。"""

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """Blob を保存し、size/sha256 を含む metadata を返す。"""

        ...

    async def get(self, key: str) -> bytes:
        """Blob 正文を返す。存在しなければ BlobNotFoundError を送出する。"""

        ...

    async def delete(self, key: str) -> None:
        """Blob を削除する。存在しなくてもエラーにしない。"""

        ...

    async def exists(self, key: str) -> bool:
        """Blob の存在有無を返す。"""

        ...

    async def stat(self, key: str) -> StoredBlob:
        """Blob metadata を返す。存在しなければ BlobNotFoundError を送出する。"""

        ...


def sanitize_object_key(key: str) -> str:
    """相対 POSIX key に限定し、path 逃逸・絶対・dot segment・制御文字を拒否する。"""

    candidate = key.strip()
    if not candidate or candidate.endswith("/") or len(candidate) > _KEY_MAX:
        raise FileStorageError("Object key is empty or too long")
    if "\\" in candidate:
        raise FileStorageError("Object key must not contain backslashes")
    path = PurePosixPath(candidate)
    if path.is_absolute() or not path.parts:
        raise FileStorageError("Object key must be a non-empty relative path")
    for part in path.parts:
        if part in {"", ".", ".."} or len(part) > _SEGMENT_MAX or not part.isprintable():
            raise FileStorageError("Object key contains an unsafe segment")
    return path.as_posix()
