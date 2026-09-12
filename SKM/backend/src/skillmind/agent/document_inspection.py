"""凍結文書の実 storage metadata を本文と区別して公開する read-only Provider。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from skillmind.agent.document_provider import resolve_frozen_document
from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.documents.domain import DocumentContentError
from skillmind.documents.snapshot import DOCUMENT_INSPECT_CAPABILITY, DocumentSnapshotError
from skillmind.documents.source import (
    InspectableProjectDocumentSource,
    ProjectDocumentObservation,
    ProjectDocumentSource,
)
from skillmind.storage import FileStorageError


class DocumentInspectProvider:
    """本文を取得せず、原 Project/ID と実 HEAD の metadata を Evidence に記録する。"""

    def __init__(self, source: ProjectDocumentSource) -> None:
        """通常文書と同じ source を使い、別の接続・権限経路を作らない。"""

        self._source = source

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """明示された inspect 権と元選択を検証し、本文 hash 未検証の観測だけを返す。"""

        if (
            context.run is None
            or context.tool.capability != DOCUMENT_INSPECT_CAPABILITY
            or DOCUMENT_INSPECT_CAPABILITY
            not in context.run.permission_snapshot.get("allowed_capabilities", [])
        ):
            raise ToolProviderError(
                "scope_denied", "Document inspection is not allowed", retryable=False
            )
        document = resolve_frozen_document(context, arguments)
        if not isinstance(self._source, InspectableProjectDocumentSource):
            raise ToolProviderError(
                "unavailable", "Storage metadata is unavailable", retryable=False
            )
        try:
            async with asyncio.timeout(20):
                observed = await self._source.inspect(
                    project_id=context.project_id, document_id=document.document_id
                )
        except (
            DocumentContentError,
            DocumentSnapshotError,
            FileStorageError,
            TimeoutError,
        ) as error:
            raise ToolProviderError(
                "unavailable", "Storage metadata is unavailable", retryable=False
            ) from error
        if (
            observed is None
            or observed.project_id != context.project_id
            or observed.document != document
            or observed.observation.size != document.size
            or resolve_frozen_document(context, arguments) != document
        ):
            raise ToolProviderError(
                "unavailable", "Frozen document metadata no longer matches", retryable=False
            )
        return document_observation_result(context.project_id, observed)


def document_observation_result(
    project_id: UUID, observed: ProjectDocumentObservation
) -> ProviderToolResult:
    """単一観測と一覧が同じ metadata JSON/hash/Evidence codec を使う。"""

    document = observed.document
    storage = observed.observation
    observation: dict[str, Any] = {
        "document": document.to_json(),
        "storage": {
            "last_modified": storage.last_modified.isoformat(),
            "version_id": storage.version_id,
            "etag": storage.etag,
            "size": storage.size,
            "content_type": storage.content_type,
        },
        "observed_at": datetime.now(UTC).isoformat(),
        "content_verified": False,
    }
    checksum = f"sha256:{sha256_hex(canonical_json(observation))}"
    return ProviderToolResult(
        response={
            "status": "success",
            "provider": "project",
            **observation,
            "observation_checksum": checksum,
            "warnings": [
                "Storage metadata only; the document bytes and declared content hash have not "
                "been verified. This observation does not prove download or conversion success."
            ],
        },
        evidence=(
            EvidenceDraft(
                evidence_type="document",
                source_uri=f"document://projects/{project_id}/{document.document_id}/metadata",
                source_locator={
                    "document_id": str(document.document_id),
                    "path": document.path,
                },
                content_hash=checksum,
                excerpt=(
                    "Storage metadata observation only; document bytes have not been verified."
                ),
                metadata={
                    "observation_version": "v1",
                    "observation": observation,
                    "source_reference_checksum": observed.reference_checksum,
                },
            ),
        ),
    )
