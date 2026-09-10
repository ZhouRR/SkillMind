"""Blob 存储の SDK 非依存な契約、metadata、object key 安全化を定義する。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol
from uuid import UUID

_KEY_MAX = 512
_SEGMENT_MAX = 128


class FileStorageError(RuntimeError):
    """Blob 存储操作の基底エラー。"""


class BlobNotFoundError(FileStorageError):
    """指定 key の blob が存在しないことを表す。"""


class BlobReadLimitExceededError(FileStorageError):
    """実際の正文が呼出元の読取上限を超えたことを表す。"""


@dataclass(frozen=True, slots=True)
class StorageNamespace:
    """Credential と独立した保存先世代。durable は実保存の回復保証を意味しない。"""

    namespace_id: UUID
    descriptor_checksum: str
    durable: bool

    def __post_init__(self) -> None:
        """不完全な DB 値や bool の暗黙変換で別の保存先を許可しない。"""

        if (
            not isinstance(self.namespace_id, UUID)
            or self.namespace_id.int == 0
            or not isinstance(self.descriptor_checksum, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.descriptor_checksum) is None
            or type(self.durable) is not bool
        ):
            raise FileStorageError("Storage namespace is invalid")


@dataclass(frozen=True, slots=True)
class BlobReference:
    """原 key と保存先を一緒に渡す内部参照。未関連の旧行は namespace=None。"""

    key: str
    namespace: StorageNamespace | None


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """保存済み blob の識別子と metadata。正文そのものは保持しない。"""

    key: str
    size: int
    content_type: str
    sha256: str


class FileStorage(Protocol):
    """Project scoped blob の非同期 put/get/delete/stat 契約。実装は差し替え可能。"""

    @property
    def namespace(self) -> StorageNamespace | None:
        """この client の固定保存先を返す。未設定から旧参照の所属を推測しない。"""

        ...

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """Blob を保存し、size/sha256 を含む metadata を返す。"""

        ...

    async def get(self, key: str, *, max_bytes: int | None = None) -> bytes:
        """欠落は BlobNotFoundError、上限 + 1 byte の検出は BlobReadLimitExceededError。

        max_bytes=None は既存呼出元の無制限読取を維持する。超過は切り詰めて返さない。
        """

        ...

    async def delete(self, key: str) -> None:
        """削除を試みる。明確な対象欠落は許容し、拒否や結果不明は成功にしない。"""

        ...

    async def exists(self, key: str) -> bool:
        """対象の存在有無を返す。存否不明や bucket の欠落は FileStorageError。"""

        ...

    async def stat(self, key: str) -> StoredBlob:
        """Blob metadata を返す。存在しなければ BlobNotFoundError を送出する。"""

        ...


def validate_read_limit(max_bytes: int | None) -> None:
    """負数や bool によって有界読取が無制限へ変化することを防ぐ。"""

    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise FileStorageError("Blob read limit must be a non-negative integer")


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
