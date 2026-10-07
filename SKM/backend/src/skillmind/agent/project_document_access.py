"""Run に固定した Project 全体の読取権と、現在の文書取得を共有する。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from skillmind.agent.tool_gateway import RunToolContext, ToolProviderError
from skillmind.documents.domain import DocumentNotFoundError
from skillmind.documents.source import ProjectDocumentSource, ProjectLibraryDocumentSource


def has_project_document_read(context: RunToolContext) -> bool:
    """旧 Run の選択権を拡張せず、明示した Project の読取権だけを使う。"""
    run = context.run
    if run is None or (
        run.run_id != context.run_id
        or run.project_id != context.project_id
        or run.run_attempt_id != context.run_attempt_id
        or run.user_id != context.user_id
    ):
        raise ToolProviderError("scope_denied", "Frozen Run identity is required", retryable=False)
    scope = run.permission_snapshot.get("project_document_read")
    if scope is None:
        return False
    if scope != {"version": "v1", "project_id": str(context.project_id)} or (
        context.tool.capability not in run.permission_snapshot.get("allowed_capabilities", [])
    ):
        raise ToolProviderError(
            "scope_denied", "Project document scope is invalid", retryable=False
        )
    return True


async def project_document_source(
    source: ProjectDocumentSource,
    context: RunToolContext,
) -> ProjectLibraryDocumentSource:
    """I/O 前後で現在のユーザー・所属を共有 Project 認可で確認する。"""
    if not has_project_document_read(context):
        raise ToolProviderError(
            "scope_denied", "Project document reading is not allowed", retryable=False
        )
    if not isinstance(source, ProjectLibraryDocumentSource):
        raise ToolProviderError(
            "unavailable", "Project document library is unavailable", retryable=False
        )
    try:
        await source.authorize_reader(project_id=context.project_id, user_id=context.user_id)
    except DocumentNotFoundError as error:
        raise ToolProviderError(
            "not_found", "Document is not accessible", retryable=False
        ) from error
    return source


def document_selector(arguments: Mapping[str, Any]) -> tuple[str | None, UUID | None]:
    """path と ID を相互排他にし、URL や別 Project を選択子として受け取らない。"""
    path, document_id = arguments.get("path"), arguments.get("document_id")
    if (path is None) == (document_id is None):
        raise ToolProviderError(
            "invalid_request", "Use either path or document_id", retryable=False
        )
    if document_id is not None:
        try:
            return None, UUID(document_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise ToolProviderError(
                "invalid_request", "Document ID is invalid", retryable=False
            ) from error
    if not isinstance(path, str):
        raise ToolProviderError("invalid_request", "Document path is invalid", retryable=False)
    return path, None
