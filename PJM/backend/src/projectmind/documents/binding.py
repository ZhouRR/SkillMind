"""文書選択を作成 transaction 内で解決し、Run 用の資源 snapshot を構築する。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from projectmind.documents.domain import DocumentNotFoundError
from projectmind.documents.repository import DocumentRepository
from projectmind.documents.snapshot import (
    DOCUMENT_PROVIDER,
    DOCUMENT_READ_CAPABILITY,
    DocumentSnapshotError,
    freeze_document_snapshot,
    parse_document_selection,
)


async def resolve_document_binding(
    repository: DocumentRepository, *, project_id: UUID, requirement_key: str, token: str
) -> dict[str, Any]:
    """全集も明示選択だけを受理し、Project 所有権と具体 ID を作成時に固定する。"""

    selection = parse_document_selection(token)
    try:
        if selection.mode == "ALL":
            documents = await repository.list_for_project(project_id)
        else:
            documents = [
                await repository.get(project_id=project_id, document_id=document_id)
                for document_id in selection.document_ids
            ]
    except DocumentNotFoundError as error:
        raise DocumentSnapshotError("Selected document is not available in this project") from error
    snapshot = freeze_document_snapshot(
        project_id=project_id,
        requirement_key=requirement_key,
        selection=selection,
        documents=documents,
    )
    return {
        "capability": DOCUMENT_READ_CAPABILITY,
        "provider": DOCUMENT_PROVIDER,
        "candidate_key": token,
        "resource_kind": "document",
        "binding_level": "RUN_DOCUMENTS",
        "access": "read",
        "document_snapshot": snapshot.to_json(),
    }
