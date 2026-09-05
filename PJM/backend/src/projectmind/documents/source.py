"""文書を data source として読む port と、その database/object storage 実装を提供する。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.documents.repository import DocumentRepository
from projectmind.storage import FileStorage


@dataclass(frozen=True, slots=True)
class ProjectDocumentContent:
    """Provider が data source として読む文書の metadata と本文 (blob 正文込み)。"""

    folder: str
    name: str
    mime: str
    checksum: str
    size: int
    data: bytes


class ProjectDocumentSource(Protocol):
    """Project 作用域で folder/name に一致する文書を解決する read-only port。"""

    async def fetch(
        self, *, project_id: UUID, folder: str, name: str
    ) -> ProjectDocumentContent | None:
        """一致する文書の内容を返す。存在しなければ None (越権も同様に None)。"""

        ...


class ProjectDocumentInventory(Protocol):
    """Project 内の全文書内容を列挙する read-only port (資源快照物化用, 計画 §19 W3)。"""

    async def list_contents(
        self, *, project_id: UUID
    ) -> Sequence[ProjectDocumentContent]:
        """Project 作用域の全文書を安定順で内容込みに返す。越権分は含めない。"""

        ...


class DatabaseProjectDocumentInventory:
    """既存の list_for_project と ProjectDocumentSource.fetch を合成する本番 inventory。

    新しい取得経路を作らず、列挙 (metadata) と本文解決 (fetch) を組み合わせる。fetch は
    project 作用域に閉じており、越権・不整合は None として静かに落ちる。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        source: ProjectDocumentSource,
    ) -> None:
        """列挙用 session factory と本文解決用 source を保持する。"""

        self._session_factory = session_factory
        self._source = source

    async def list_contents(
        self, *, project_id: UUID
    ) -> Sequence[ProjectDocumentContent]:
        """folder/name 昇順で列挙し、各文書の本文を fetch して返す。"""

        async with self._session_factory() as session:
            documents = await DocumentRepository(session).list_for_project(project_id)
        contents: list[ProjectDocumentContent] = []
        for document in documents:
            content = await self._source.fetch(
                project_id=project_id, folder=document.folder, name=document.name
            )
            if content is not None:
                contents.append(content)
        return contents


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

    async def fetch(
        self, *, project_id: UUID, folder: str, name: str
    ) -> ProjectDocumentContent | None:
        """Project 作用域で metadata を引き、blob 正文を読み出す。不存在は None。"""

        async with self._session_factory() as session:
            found = await DocumentRepository(session).find_by_path(
                project_id=project_id, folder=folder, name=name
            )
        if found is None:
            return None
        document, storage_key = found
        data = await self._file_storage.get(storage_key)
        return ProjectDocumentContent(
            folder=document.folder,
            name=document.name,
            mime=document.mime,
            checksum=document.checksum,
            size=document.size,
            data=data,
        )
