"""文書管理の観測版を path/元 ID/byte hash/回収状態から決定する。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import ProjectDocument, ProjectDocumentFolder

FILE_OPERATIONS = frozenset(
    {"MOVE", "MOVE_FOLDER", "CREATE_FOLDER", "DELETE_FOLDER", "TRASH", "RESTORE"}
)
FOLDER_OPERATIONS = frozenset({"CREATE_FOLDER", "MOVE_FOLDER", "DELETE_FOLDER"})


def file_description(document: ProjectDocument) -> dict[str, Any]:
    """資格情報や物理 key を含めず、画面と同じ論理 file の識別子を返す。"""
    return {
        "document_id": str(document.id),
        "path": "/".join(filter(None, (document.folder, document.name))),
        "content_hash": document.checksum,
        "size_bytes": document.size,
        "mime_type": document.mime,
        "trashed": document.deleted_at is not None,
    }


def state_revision(value: dict[str, Any]) -> str:
    """同じ観測値だけを同じ版とし、意味のない時刻更新に依存しない。"""
    return "sha256:" + sha256_hex(canonical_json(value))


async def observe_file_state(
    session: AsyncSession,
    *,
    project_id: UUID,
    path: str,
    folder: bool = False,
    trashed: bool = False,
    document_id: str | UUID | None = None,
) -> dict[str, Any]:
    """確認対象だけを返す。ディレクトリは子集合全体を含む版で同時変更を検出する。"""
    value: dict[str, Any]
    if not folder:
        parent, _, name = path.rpartition("/")
        rows = list(
            await session.scalars(
                select(ProjectDocument)
                .where(
                    ProjectDocument.project_id == project_id,
                    ProjectDocument.folder == parent,
                    ProjectDocument.name == name,
                    ProjectDocument.deleted_at.is_not(None)
                    if trashed
                    else ProjectDocument.deleted_at.is_(None),
                )
                .order_by(ProjectDocument.id)
            )
        )
        if document_id is not None:
            rows = [row for row in rows if str(row.id) == str(document_id)]
        if len(rows) > 1:
            raise ValueError(
                "Multiple recycled versions match; use an unambiguous document identity"
            )
        value = {
            "kind": "file",
            "path": path,
            "document": file_description(rows[0]) if rows else None,
        }
    else:
        documents = list(
            await session.scalars(
                select(ProjectDocument)
                .where(
                    ProjectDocument.project_id == project_id,
                    ProjectDocument.deleted_at.is_(None),
                )
                .order_by(ProjectDocument.id)
            )
        )
        directories = list(
            await session.scalars(
                select(ProjectDocumentFolder.path)
                .where(
                    ProjectDocumentFolder.project_id == project_id,
                )
                .order_by(ProjectDocumentFolder.path)
            )
        )
        children = [
            file_description(d)
            for d in documents
            if d.folder == path or d.folder.startswith(path + "/")
        ]
        nested = [p for p in directories if p == path or p.startswith(path + "/")]
        value = {
            "kind": "directory",
            "path": path,
            "exists": bool(children or nested),
            "documents": children,
            "directories": nested,
        }
    return {**value, "revision": state_revision(value)}
