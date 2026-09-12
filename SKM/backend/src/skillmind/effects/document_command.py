"""書込と原回执照会で、同じ Artifact と保存先から原文書要求を構築する。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.artifacts.repository import ArtifactRepository
from skillmind.documents.library import DocumentLibraryTarget
from skillmind.storage.effect_write import ObjectWriteCommand, build_object_write


async def load_document_effect_command(
    session: AsyncSession,
    *,
    effect_id: UUID,
    project_id: UUID,
    run_id: UUID,
    payload: dict[str, Any],
    target: DocumentLibraryTarget,
) -> ObjectWriteCommand:
    """共有提案 validator の payload と、同 Run の保存済み実 byte だけを受け付ける。"""

    artifact = await ArtifactRepository(session).get_content(
        project_id=project_id,
        run_id=run_id,
        artifact_ref=payload["artifact_ref"],
    )
    if artifact is None or (artifact.metadata.checksum, artifact.metadata.size_bytes) != (
        payload["content_hash"],
        payload["size_bytes"],
    ):
        raise ValueError("Approved document Artifact is unavailable")
    return build_object_write(
        effect_id=effect_id,
        project_id=project_id,
        run_id=run_id,
        artifact=artifact,
        namespace=target.namespace,
        bucket=target.bucket,
        object_key=payload["object_key"],
        allowed_prefix=target.scope(project_id)["key_prefix"],
        content_type=payload["mime_type"],
    )
