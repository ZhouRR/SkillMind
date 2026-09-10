"""Project 文書を document.read/v1 data source として投影する Provider を実装する。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.text_window import select_line_window
from skillmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolProviderError,
)
from skillmind.documents.snapshot import (
    DocumentSnapshotError,
    selected_document_snapshots,
    snapshot_documents,
)
from skillmind.documents.source import ProjectDocumentSource, read_frozen_document


class DocumentProvider:
    """Project 作用域の文書を UTF-8 text として読む read-only Provider。"""

    def __init__(self, source: ProjectDocumentSource) -> None:
        """文書内容を解決する project 作用域 source を保持する。"""

        self._source = source

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """Run の project 内で path に一致する文書を、行範囲と content hash 付きで返す。"""

        folder, name = _split_document_path(arguments.get("path"))
        if context.run is None or (
            context.run.run_id != context.run_id
            or context.run.project_id != context.project_id
            or context.run.run_attempt_id != context.run_attempt_id
            or context.run.user_id != context.user_id
        ):
            raise ToolProviderError(
                "scope_denied", "Frozen Run context is required", retryable=False
            )
        try:
            snapshots = selected_document_snapshots(
                context.run.resolved_sources, project_id=context.project_id
            )
            documents = snapshot_documents(snapshots)
        except DocumentSnapshotError as error:
            raise ToolProviderError("scope_denied", str(error), retryable=False) from error
        document = next(
            (item for item in documents if item.folder == folder and item.name == name), None
        )
        if document is None:
            # 未選択 path の有無を問い合わせない。Project 全体の探索へ広げないための境界。
            raise ToolProviderError("not_found", "Document was not found", retryable=False)
        try:
            content = await read_frozen_document(
                self._source, project_id=context.project_id, document=document
            )
        except DocumentSnapshotError as error:
            raise ToolProviderError("unavailable", str(error), retryable=False) from error
        try:
            text = content.data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolProviderError(
                "invalid_request", "Document is not UTF-8 text", retryable=False
            ) from error
        # 空文書も既定範囲で読めるという document.read/v1 の既存契約を保つ。
        window = select_line_window(text, arguments, subject="Document", allow_empty=True)
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "project",
                "document": {
                    "folder": content.folder,
                    "name": content.name,
                    "mime": content.mime,
                    "checksum": content.checksum,
                    "size": content.size,
                },
                "content": window.content,
                "line_start": window.line_start,
                "line_end": window.line_end,
                "warnings": ["Content was truncated"] if window.truncated else [],
                "truncated": window.truncated,
            },
            evidence=(
                EvidenceDraft(
                    # content hash で addressing し、被参照文書を不変に固定する。
                    evidence_type="document",
                    source_uri=f"document://projects/{context.project_id}/{content.checksum}",
                    source_locator={
                        "document_id": str(document.document_id),
                        "folder": content.folder,
                        "name": content.name,
                        "line_start": window.line_start,
                        "line_end": window.line_end,
                    },
                    content_hash=content.checksum,
                    excerpt=window.content[:2_000],
                    metadata={"reproducibility": "content_hash"},
                ),
            ),
        )


def _split_document_path(value: Any) -> tuple[str, str]:
    """引数 path を安全化し、(folder, name) の相対 POSIX 分割に落とす。"""

    if not isinstance(value, str):
        raise ToolProviderError("invalid_request", "Document path is invalid", retryable=False)
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts:
        raise ToolProviderError("invalid_request", "Document path is invalid", retryable=False)
    for part in relative.parts:
        if part in {"", ".", ".."} or not part.isprintable():
            raise ToolProviderError("invalid_request", "Document path is invalid", retryable=False)
    *folder_parts, name = relative.parts
    return "/".join(folder_parts), name
