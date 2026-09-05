"""Project 文書を資源束縛の候補として列挙する ProjectResourceCatalog 実装を提供する。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.documents.repository import DocumentRepository
from projectmind.skills.resource_binding import ProjectResourceCandidate

DOCUMENT_PROVIDER = "project-documents"
DOCUMENT_READ_CAPABILITY = "document.read/v1"


class DocumentResourceCatalog:
    """登録済み文書を `document` 種別の束縛候補として返す本番 catalog。

    Integration 候補は CompositeProjectResourceCatalog の別 catalog が列挙する。ここでは
    文書の識別子と能力だけを返し、blob 正文は document Provider 側の責務として扱う。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Database session factory を保持する。"""

        self._session_factory = session_factory

    async def candidates(self, *, project_id: UUID) -> tuple[ProjectResourceCandidate, ...]:
        """Project 内文書を安定順の資源候補として返す。"""

        async with self._session_factory() as session:
            documents = await DocumentRepository(session).list_for_project(project_id)
        return tuple(
            ProjectResourceCandidate(
                key=f"{document.folder}/{document.name}",
                kind="document",
                provider=DOCUMENT_PROVIDER,
                label=f"{document.folder}/{document.name}",
                capabilities=(DOCUMENT_READ_CAPABILITY,),
            )
            for document in documents
        )
