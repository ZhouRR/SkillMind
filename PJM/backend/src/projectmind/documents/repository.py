"""Project 作用域の文書 metadata 永続化を実装する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.db.models import ProjectDocument
from projectmind.documents.domain import (
    DocumentNotFoundError,
    StoredDocument,
    UploadDocumentCommand,
)


class DocumentRepository:
    """Transaction-scoped session 上で文書 metadata を読み書きする。"""

    def __init__(self, session: AsyncSession) -> None:
        """Transaction-scoped database session を保持する。"""

        self._session = session

    async def project_usage_bytes(self, project_id: UUID) -> int:
        """配額判定用に Project 内文書 size の合計を返す。"""

        statement = select(func.coalesce(func.sum(ProjectDocument.size), 0)).where(
            ProjectDocument.project_id == project_id
        )
        return int(await self._session.scalar(statement) or 0)

    async def create(self, command: UploadDocumentCommand) -> StoredDocument:
        """文書 metadata 行を追加し、read model を返す。"""

        now = datetime.now(UTC)
        document = ProjectDocument(
            id=command.document_id,
            project_id=command.project_id,
            folder=command.folder,
            name=command.name,
            storage_key=command.storage_key,
            size=command.size,
            mime=command.mime,
            checksum=command.checksum,
            uploaded_by=command.uploaded_by,
            created_at=now,
        )
        self._session.add(document)
        return _to_stored(document)

    async def list_for_project(self, project_id: UUID) -> list[StoredDocument]:
        """Project 内文書を folder/name 昇順で列挙する。"""

        statement = (
            select(ProjectDocument)
            .where(ProjectDocument.project_id == project_id)
            .order_by(ProjectDocument.folder, ProjectDocument.name)
        )
        return [_to_stored(row) for row in await self._session.scalars(statement)]

    async def find_by_path(
        self, *, project_id: UUID, folder: str, name: str
    ) -> tuple[StoredDocument, str] | None:
        """Project 作用域で folder/name に一致する文書と storage_key を返す。無ければ None。"""

        statement = select(ProjectDocument).where(
            ProjectDocument.project_id == project_id,
            ProjectDocument.folder == folder,
            ProjectDocument.name == name,
        )
        document = (await self._session.scalars(statement)).first()
        if document is None:
            return None
        return _to_stored(document), document.storage_key

    async def get(self, *, project_id: UUID, document_id: UUID) -> StoredDocument:
        """所有 Project 内の文書 metadata を取得する。越権/不存在は 404 相当へ畳む。"""

        return _to_stored(await self._require(project_id=project_id, document_id=document_id))

    async def get_for_download(
        self, *, project_id: UUID, document_id: UUID
    ) -> tuple[StoredDocument, str]:
        """Download 用に metadata と内部 storage_key を所有確認付きで返す。"""

        document = await self._require(project_id=project_id, document_id=document_id)
        return _to_stored(document), document.storage_key

    async def delete(self, *, project_id: UUID, document_id: UUID) -> str:
        """文書 metadata 行を削除し、blob 削除用の storage_key を返す。"""

        document = await self._require(project_id=project_id, document_id=document_id)
        storage_key = document.storage_key
        await self._session.delete(document)
        return storage_key

    async def _require(self, *, project_id: UUID, document_id: UUID) -> ProjectDocument:
        """Project 所有を確認して文書行を返す。越権と不存在は同じ error へ畳む。"""

        document = await self._session.get(ProjectDocument, document_id)
        if document is None or document.project_id != project_id:
            raise DocumentNotFoundError(f"Document not found: {document_id}")
        return document


def _to_stored(document: ProjectDocument) -> StoredDocument:
    """ORM 行を storage 非依存の read model へ変換する。"""

    return StoredDocument(
        document_id=document.id,
        project_id=document.project_id,
        folder=document.folder,
        name=document.name,
        size=document.size,
        mime=document.mime,
        checksum=document.checksum,
        uploaded_by=document.uploaded_by,
        created_at=document.created_at,
    )
