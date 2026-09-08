"""凍結した文書 metadata と本文を共有する検証用 fixture を定義する。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from projectmind.core.hashing import sha256_hex
from projectmind.documents.domain import StoredDocument
from projectmind.documents.snapshot import DocumentSnapshot, FrozenDocument
from projectmind.documents.source import ProjectDocumentContent


def document_content(
    data: bytes = b"# Overview\nsecond line\n",
    *,
    folder: str = "specs",
    name: str = "overview.md",
    mime: str = "text/markdown",
    document_id: UUID | None = None,
) -> ProjectDocumentContent:
    """path ごとに安定した identity と実 byte に対応する checksum を返す。"""

    return ProjectDocumentContent(
        document_id=document_id
        or uuid5(NAMESPACE_URL, f"projectmind:test-document:{folder}/{name}"),
        folder=folder,
        name=name,
        mime=mime,
        checksum=f"sha256:{sha256_hex(data)}",
        size=len(data),
        data=data,
    )


def stored_document(project_id: UUID, content: ProjectDocumentContent) -> StoredDocument:
    """所有 Project を明示した metadata を本文から作る。"""

    return StoredDocument(
        document_id=content.document_id,
        project_id=project_id,
        folder=content.folder,
        name=content.name,
        size=content.size,
        mime=content.mime,
        checksum=content.checksum,
        uploaded_by=UUID("30000000-0000-0000-0000-000000000001"),
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
    )


def document_snapshot(
    project_id: UUID, contents: Sequence[ProjectDocumentContent], *, key: str = "config"
) -> DocumentSnapshot:
    """テスト入力の具体集合を固定する。Runtime が Project 全集へ退避する fake ではない。"""

    return DocumentSnapshot(
        project_id,
        key,
        "ALL",
        tuple(
            FrozenDocument(
                content.document_id,
                content.folder,
                content.name,
                content.mime,
                content.size,
                content.checksum,
            )
            for content in sorted(contents, key=lambda content: content.document_id)
        ),
    )
