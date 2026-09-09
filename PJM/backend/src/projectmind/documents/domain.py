"""Project 文書 use case の read model、command、error を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """保存済み文書 metadata の公開 read model。blob 正文は含まない。"""

    document_id: UUID
    project_id: UUID
    folder: str
    name: str
    size: int
    mime: str
    checksum: str
    uploaded_by: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UploadDocumentCommand:
    """文書 metadata 行を追加する command。blob は事前に object storage へ保存済み。"""

    project_id: UUID
    document_id: UUID
    folder: str
    name: str
    storage_key: str
    size: int
    mime: str
    checksum: str
    uploaded_by: UUID


class DocumentNotFoundError(LookupError):
    """指定 Project から参照可能な文書が存在しないことを表す。"""


class DocumentConflictError(ValueError):
    """同一 folder/name の文書が既に存在することを表す。"""


class DocumentContentError(RuntimeError):
    """保存済み文書の正文を安全に提供できないことを表す。"""


class DocumentContentMissingError(DocumentContentError):
    """元の metadata はあるが、元の blob が存在しないことを表す。"""


class DocumentContentInvalidError(DocumentContentError):
    """正文の実 size/hash が保存された metadata と一致しないことを表す。"""


class DocumentStorageUnavailableError(DocumentContentError):
    """存否を断定できない storage の拒否・通信障害を表す。"""


class DocumentInUseError(ValueError):
    """原文書が実行・調度・保持済み発火に参照されていることを表す。"""


class DocumentReferencesUnavailableError(ValueError):
    """歴史の欠落や破損のため、無参照を安全に確認できないことを表す。"""
