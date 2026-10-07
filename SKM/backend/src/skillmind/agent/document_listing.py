"""Project または旧 Run の凍結文書集合を有界 page で観測する。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from skillmind.agent.document_inspection import document_observation_result
from skillmind.agent.document_provider import resolve_frozen_documents
from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.project_document_access import (
    has_project_document_read,
    project_document_source,
)
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.documents.directory_query import parse_directory_query
from skillmind.documents.domain import DocumentContentError
from skillmind.documents.snapshot import (
    DOCUMENT_LIST_CAPABILITY,
    DocumentSnapshotError,
    FrozenDocument,
    same_document_content,
)
from skillmind.documents.source import InspectableProjectDocumentSource, ProjectDocumentSource
from skillmind.storage import FileStorageError


class DocumentListProvider:
    """Run の読取範囲内で page と文書ごとの再利用可能な観測を返す。"""

    def __init__(self, source: ProjectDocumentSource) -> None:
        """単一 inspect と同じ source を使い、bucket の別権限経路を作らない。"""

        self._source = source

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """path 条件を先に適用し、選択した page の実 metadata だけを取得する。"""

        project_read = has_project_document_read(context)
        if project_read:
            library = await project_document_source(self._source, context)
            documents = tuple(await library.list_documents(project_id=context.project_id))
            # 現在目録は本文を追加取得する権限であり、試験入力の集合を変更しない。
            _listing_documents(context)
        else:
            documents = _listing_documents(context)
        try:
            query = parse_directory_query(arguments)
            limit = arguments.get("limit", 50)
            if type(limit) is not int or not 1 <= limit <= 50:
                raise ValueError("Page limit is invalid")
            checksum = "sha256:" + sha256_hex(
                canonical_json(
                    {
                        "version": "v1",
                        "run_id": str(context.run_id),
                        "project_id": str(context.project_id),
                        "query": query.parameters,
                        "limit": limit,
                        "documents": [item.to_json() for item in documents],
                    }
                )
            )
            candidates = query.candidates(documents)
            start = _page_start(arguments, checksum, len(candidates), limit)
        except ValueError as error:
            raise ToolProviderError(
                "invalid_request", "Directory query or cursor is invalid", retryable=False
            ) from error
        if not isinstance(self._source, InspectableProjectDocumentSource):
            raise ToolProviderError(
                "unavailable", "Storage metadata is unavailable", retryable=False
            )
        end = min(start + limit, len(candidates))
        entries: list[dict[str, Any]] = []
        evidence: list[EvidenceDraft] = []
        try:
            async with asyncio.timeout(30):
                for document in candidates[start:end]:
                    observed = await self._source.inspect(
                        project_id=context.project_id, document_id=document.document_id
                    )
                    if (
                        observed is None
                        or observed.project_id != context.project_id
                        or not same_document_content(observed.document, document)
                        or observed.observation.size != document.size
                        or (not project_read and _listing_documents(context) != documents)
                    ):
                        raise DocumentSnapshotError("Frozen document metadata no longer matches")
                    result = document_observation_result(
                        context.project_id, replace(observed, document=document)
                    )
                    entries.append(
                        {
                            **{
                                key: value
                                for key, value in result.response.items()
                                if key not in {"status", "provider", "warnings"}
                            },
                            "matches_filter": query.matches_time(
                                observed.observation.last_modified
                            ),
                            "evidence_index": len(entries) + 1,
                        }
                    )
                    evidence.extend(result.evidence)
        except (
            DocumentContentError,
            DocumentSnapshotError,
            FileStorageError,
            TimeoutError,
        ) as error:
            # 部分成功を公開すると、失敗した文書を対象なしと誤認するため page 全体を失敗させる。
            raise ToolProviderError(
                "unavailable", "Directory metadata observation is incomplete", retryable=False
            ) from error
        if project_read:
            await project_document_source(self._source, context)
        elif _listing_documents(context) != documents:
            raise ToolProviderError("scope_denied", "Frozen selection changed", retryable=False)
        response: dict[str, Any] = {
            "status": "success",
            "provider": "project",
            "scope": "project_documents" if project_read else "run_frozen_documents",
            "query": query.parameters,
            "query_checksum": checksum,
            "limit": limit,
            "frozen_count": len(documents),
            "candidate_count": len(candidates),
            "scan_start": start,
            "scan_end": end,
            "next_cursor": {"query_checksum": checksum, "offset": end}
            if end < len(candidates)
            else None,
            "entries": entries,
            "warnings": [
                *(
                    [
                        "Current project document library, excluding the recycle bin. Additional "
                        "references do not change frozen task inputs. If the catalog changes "
                        "between "
                        "pages, start a new listing without cursor."
                    ]
                    if project_read
                    else [
                        "Only documents frozen and authorized for this Run are covered; this is "
                        "not a live bucket listing. Follow next_cursor until null, including pages "
                        "with "
                        "no matching entries. Failed pages do not establish coverage."
                    ]
                ),
                "Storage metadata only; document bytes and declared content hashes are not "
                "verified. For conversion use evidence_refs[entry.evidence_index] as "
                "observation_ref. Date filtering does not grant additional access.",
            ],
        }
        summary = EvidenceDraft(
            evidence_type="document_listing",
            source_uri=f"document://projects/{context.project_id}/runs/{context.run_id}/listing",
            source_locator={"query_checksum": checksum, "scan_start": start, "scan_end": end},
            content_hash="sha256:" + sha256_hex(canonical_json(response)),
            excerpt="Bounded project metadata page; no bytes verified.",
            metadata={"listing_version": "v1", "page": response},
        )
        return ProviderToolResult(response=response, evidence=(summary, *evidence))


def _page_start(arguments: Mapping[str, Any], checksum: str, count: int, limit: int) -> int:
    """cursor を原 Run・選択全体・正規化条件・page 幅へ結び、別条件の継続を拒否する。"""

    if "cursor" not in arguments:
        return 0
    cursor = arguments["cursor"]
    if not isinstance(cursor, Mapping) or set(cursor) != {"query_checksum", "offset"}:
        raise ValueError("Cursor is invalid")
    offset = cursor["offset"]
    if (
        cursor["query_checksum"] != checksum
        or type(offset) is not int
        or not 0 < offset < count
        or offset % limit != 0
    ):
        raise ValueError("Cursor does not match this selection")
    return offset


def _listing_documents(context: RunToolContext) -> tuple[FrozenDocument, ...]:
    """await の前後で同じ明示能力・Run identity・原選択を検証する。"""

    if (
        context.run is None
        or context.tool.capability != DOCUMENT_LIST_CAPABILITY
        or DOCUMENT_LIST_CAPABILITY
        not in context.run.permission_snapshot.get("allowed_capabilities", [])
    ):
        raise ToolProviderError("scope_denied", "Document listing is not allowed", retryable=False)
    return resolve_frozen_documents(context)
