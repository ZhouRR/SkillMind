"""Project 文書を資源束縛の候補として列挙する ProjectResourceCatalog 実装を提供する。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.documents.repository import DocumentRepository
from projectmind.documents.snapshot import (
    ALL_DOCUMENTS_SELECTION,
    DOCUMENT_PROVIDER,
    DOCUMENT_READ_CAPABILITY,
)
from projectmind.skills.resource_binding import ProjectResourceCandidate


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
        individual = tuple(
            ProjectResourceCandidate(
                key=f"document:{document.document_id}",
                kind="document",
                provider=DOCUMENT_PROVIDER,
                label="/".join(part for part in (document.folder, document.name) if part),
                capabilities=(DOCUMENT_READ_CAPABILITY,),
                scope={"selection_mode": "SINGLE", "document_ids": [str(document.document_id)]},
            )
            for document in documents
        )
        if not individual:
            return ()
        return (
            *individual,
            ProjectResourceCandidate(
                key=ALL_DOCUMENTS_SELECTION,
                kind="document",
                provider=DOCUMENT_PROVIDER,
                label="All project documents (membership frozen at Run creation)",
                capabilities=(DOCUMENT_READ_CAPABILITY,),
                scope={"selection_mode": "ALL"},
            ),
        )
