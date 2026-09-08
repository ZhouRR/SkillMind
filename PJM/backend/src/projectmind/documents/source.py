"""文書を data source として読む port と、その database/object storage 実装を提供する。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.core.hashing import sha256_hex
from projectmind.documents.domain import DocumentNotFoundError
from projectmind.documents.repository import DocumentRepository
from projectmind.documents.snapshot import DocumentSnapshotError, FrozenDocument
from projectmind.storage import FileStorage, FileStorageError


@dataclass(frozen=True, slots=True)
class ProjectDocumentContent:
    """Provider が data source として読む文書の metadata と本文 (blob 正文込み)。"""

    document_id: UUID
    folder: str
    name: str
    mime: str
    checksum: str
    size: int
    data: bytes


class ProjectDocumentSource(Protocol):
    """Project と不変 ID で文書を解決する read-only port。"""

    async def fetch(self, *, project_id: UUID, document_id: UUID) -> ProjectDocumentContent | None:
        """一致する文書の内容を返す。存在しなければ None (越権も同様に None)。"""

        ...


class ProjectDocumentInventory(Protocol):
    """Run に凍結済みの文書だけを内容付きで取得する port。"""

    async def list_contents(
        self, *, project_id: UUID, documents: Sequence[FrozenDocument]
    ) -> Sequence[ProjectDocumentContent]:
        """選択済み ID を安定順で取得し、欠落や置換を黙って省略しない。"""

        ...


class DatabaseProjectDocumentInventory:
    """Project を再列挙せず、凍結した ID/hash を共通 source で検証する。"""

    def __init__(
        self,
        *,
        source: ProjectDocumentSource,
    ) -> None:
        """本文解決用 source を保持し、別の資源認可経路を作らない。"""

        self._source = source

    async def list_contents(
        self, *, project_id: UUID, documents: Sequence[FrozenDocument]
    ) -> Sequence[ProjectDocumentContent]:
        """一件でも削除・改変・欠損していれば明示的に失敗させる。"""

        return [
            await read_frozen_document(self._source, project_id=project_id, document=document)
            for document in documents
        ]


class DatabaseProjectDocumentSource:
    """DocumentRepository と FileStorage を用いて文書内容を読み出す本番 source。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        file_storage: FileStorage,
    ) -> None:
        """Database factory と object storage backend を保持する。"""

        self._session_factory = session_factory
        self._file_storage = file_storage

    async def fetch(self, *, project_id: UUID, document_id: UUID) -> ProjectDocumentContent | None:
        """Project と ID の一致を確認して blob を読み、同名の別文書へ切り替えない。"""

        async with self._session_factory() as session:
            try:
                document, storage_key = await DocumentRepository(session).get_for_download(
                    project_id=project_id, document_id=document_id
                )
            except DocumentNotFoundError:
                return None
        data = await self._file_storage.get(storage_key)
        return ProjectDocumentContent(
            document_id=document.document_id,
            folder=document.folder,
            name=document.name,
            mime=document.mime,
            checksum=document.checksum,
            size=document.size,
            data=data,
        )


async def read_frozen_document(
    source: ProjectDocumentSource, *, project_id: UUID, document: FrozenDocument
) -> ProjectDocumentContent:
    """Provider と物化器で ID/hash の確認を共有し、storage の内部情報を外へ出さない。"""

    try:
        content = await source.fetch(project_id=project_id, document_id=document.document_id)
    except FileStorageError as error:
        raise DocumentSnapshotError("Frozen document content is unavailable") from error
    if content is None:
        raise DocumentSnapshotError("Frozen document is no longer available")
    verify_frozen_content(document, content)
    return content


def verify_frozen_content(document: FrozenDocument, content: ProjectDocumentContent) -> None:
    """申告 hash だけを信頼せず、凍結 metadata と実 byte の一致を検証する。"""

    if (
        content.document_id != document.document_id
        or content.folder != document.folder
        or content.name != document.name
        or content.mime != document.mime
        or content.size != document.size
        or len(content.data) != document.size
        or content.checksum != document.content_hash
        or f"sha256:{sha256_hex(content.data)}" != document.content_hash
    ):
        raise DocumentSnapshotError("Frozen document content no longer matches its snapshot")
