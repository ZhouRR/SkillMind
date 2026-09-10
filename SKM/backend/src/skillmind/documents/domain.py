"""Project 文書 use case の read model、command、error を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from skillmind.storage.blob import StorageNamespace


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
    """PUT 前に原予約へ固定し、同じ内容だけを metadata 公開にも渡す command。"""

    project_id: UUID
    document_id: UUID
    folder: str
    name: str
    storage_key: str
    storage_namespace: StorageNamespace
    upload_intent_id: UUID
    size: int
    mime: str
    checksum: str
    uploaded_by: UUID


@dataclass(frozen=True, slots=True)
class StoredDocumentUpload:
    """原 upload の持続投影。PUBLISHED は現在の文書存在や清理の証明ではない。"""

    upload_key: UUID
    project_id: UUID
    state: Literal["PENDING", "PUBLISHED"]
    created_at: datetime
    document: StoredDocument | None


@dataclass(frozen=True, slots=True)
class StoredDocumentUploadClosure:
    """公開を閉じた独立回执。遠端 PUT の停止・清理・占用結算は表さない。"""

    upload_key: UUID
    project_id: UUID
    document_id: UUID
    closed_at: datetime
    publication_state: Literal["CLOSED"] = "CLOSED"


@dataclass(frozen=True, slots=True)
class DocumentCleanupActor:
    """認可 lock 内で確認した閉鎖・清理要求者。旧 upload の会話を補造しない。"""

    organization_id: UUID
    actor_id: UUID
    request_id: UUID
    session_id: UUID


class DocumentUploadError(RuntimeError):
    """原 upload の受付・照合・保存事実を確定できないことを表す。"""


class DocumentUploadKeyConflictError(DocumentUploadError):
    """同じ actor/Project/key に異なる本文・path の原要求が記録されている。"""


class DocumentUploadPendingError(DocumentUploadError):
    """原 intent はあるが公開回执がなく、再 PUT や占用解放を許可できない。"""


class DocumentUploadClosedError(DocumentUploadError):
    """原 intent の公開は閉じられており、PUT や metadata 公開を再開できない。"""


class DocumentUploadAlreadyPublishedError(DocumentUploadError):
    """公開が先に完了しており、閉鎖要求を文書削除にすり替えてはならない。"""


class DocumentUploadClosureNotFoundError(DocumentUploadError):
    """原閉鎖回执が現在見つからない。閉鎖 POST の将来の commit は否定しない。"""


class DocumentUploadNotFoundError(DocumentUploadError):
    """現在の actor/Project で原 intent が見つからない。旧 POST の未到達は証明しない。"""


class DocumentUploadInvalidError(DocumentUploadError):
    """持続した原要求・所属・公開回执の整合性を検証できない。"""


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
