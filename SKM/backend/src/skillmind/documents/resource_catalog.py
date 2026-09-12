"""Project 文書を資源束縛の候補として列挙する ProjectResourceCatalog 実装を提供する。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.documents.library import (
    DOCUMENT_LIBRARY_PROVIDER,
    DOCUMENT_LIBRARY_REVISION,
    DOCUMENT_LIBRARY_SELECTION,
    DOCUMENT_WRITE_CAPABILITY,
    DocumentLibraryTarget,
)
from skillmind.documents.repository import DocumentRepository
from skillmind.documents.snapshot import (
    ALL_DOCUMENTS_SELECTION,
    DOCUMENT_CAPABILITIES,
    DOCUMENT_INSPECT_CAPABILITY,
    DOCUMENT_LIST_CAPABILITY,
    DOCUMENT_PROVIDER,
    DOCUMENT_READ_CAPABILITY,
)
from skillmind.skills.resource_binding import ProjectResourceCandidate


class DocumentResourceCatalog:
    """登録済み文書を `document` 種別の束縛候補として返す本番 catalog。

    Integration 候補は CompositeProjectResourceCatalog の別 catalog が列挙する。ここでは
    文書の識別子と能力だけを返し、blob 正文は document Provider 側の責務として扱う。
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *,
        library_target: DocumentLibraryTarget | None = None,
    ) -> None:
        """Database session factory を保持する。"""

        self._session_factory = session_factory
        self._library_target = library_target

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
                capabilities=(DOCUMENT_CAPABILITIES
                              if document.name.lower().endswith((".xlsx", ".xls"))
                              else (DOCUMENT_READ_CAPABILITY, DOCUMENT_INSPECT_CAPABILITY,
                                    DOCUMENT_LIST_CAPABILITY)),
                scope={"selection_mode": "SINGLE", "document_ids": [str(document.document_id)]},
            )
            for document in documents
        )
        library = () if self._library_target is None else (
            ProjectResourceCandidate(
                key=DOCUMENT_LIBRARY_SELECTION,
                kind="document",
                provider=DOCUMENT_LIBRARY_PROVIDER,
                label="Project document library",
                capabilities=(DOCUMENT_WRITE_CAPABILITY,),
                revision=DOCUMENT_LIBRARY_REVISION,
            ),
        )
        if not individual:
            return library
        return (
            *individual,
            ProjectResourceCandidate(
                key=ALL_DOCUMENTS_SELECTION,
                kind="document",
                provider=DOCUMENT_PROVIDER,
                label="All project documents (membership frozen at Run creation)",
                capabilities=tuple(
                    item for item in DOCUMENT_CAPABILITIES
                    if any(item in candidate.capabilities for candidate in individual)
                ),
                scope={"selection_mode": "ALL"},
            ),
            *library,
        )
