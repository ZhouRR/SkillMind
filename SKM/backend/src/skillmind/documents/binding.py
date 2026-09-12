"""文書選択を作成 transaction 内で解決し、Run 用の資源 snapshot を構築する。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from skillmind.documents.domain import DocumentNotFoundError
from skillmind.documents.repository import DocumentRepository
from skillmind.documents.snapshot import (
    DOCUMENT_CAPABILITIES,
    DOCUMENT_CONVERT_CAPABILITY,
    DOCUMENT_INSPECT_CAPABILITY,
    DOCUMENT_LIST_CAPABILITY,
    DOCUMENT_PROVIDER,
    DOCUMENT_READ_CAPABILITY,
    ON_DEMAND_DOCUMENT_PREPARATION,
    DocumentSnapshotError,
    freeze_document_snapshot,
    is_document_source,
    parse_document_selection,
)


async def resolve_document_binding(
    repository: DocumentRepository, *, project_id: UUID, requirement_key: str, token: str,
    capability: str = DOCUMENT_READ_CAPABILITY,
) -> dict[str, Any]:
    """全集も明示選択だけを受理し、Project 所有権と具体 ID を作成時に固定する。"""

    if capability not in DOCUMENT_CAPABILITIES:
        raise DocumentSnapshotError("Document capability is not supported")
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
    binding: dict[str, Any] = {
        "capability": capability,
        "provider": DOCUMENT_PROVIDER,
        "candidate_key": token,
        "resource_kind": "document",
        "binding_level": "RUN_DOCUMENTS",
        "access": "read",
        "document_snapshot": snapshot.to_json(),
    }
    if capability in {
        DOCUMENT_CONVERT_CAPABILITY, DOCUMENT_INSPECT_CAPABILITY, DOCUMENT_LIST_CAPABILITY,
    }:
        binding["preparation_policy"] = ON_DEMAND_DOCUMENT_PREPARATION
    return binding


async def revalidate_document_choices(
    repository: DocumentRepository, *, project_id: UUID, sources: dict[str, str]
) -> None:
    """锁外検証済みの選択を保存門禁内で再確認し、削除後に旧 ID を保存させない。"""

    for key, token in sources.items():
        if is_document_source(token):
            # blob/Skill/Provider は読まない。ALL も現在の空集合や上限を同じ codec で検証する。
            await resolve_document_binding(
                repository, project_id=project_id, requirement_key=key, token=token
            )
