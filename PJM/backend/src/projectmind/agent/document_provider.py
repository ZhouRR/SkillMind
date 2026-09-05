"""Project 文書を document.read/v1 data source として投影する Provider を実装する。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from projectmind.agent.evidence import EvidenceDraft
from projectmind.agent.text_window import select_line_window
from projectmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolProviderError,
)
from projectmind.documents.source import ProjectDocumentSource


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
        # source は Run snapshot の project_id に閉じており、越権/不存在は同じ not_found へ畳む。
        content = await self._source.fetch(
            project_id=context.project_id, folder=folder, name=name
        )
        if content is None:
            raise ToolProviderError("not_found", "Document was not found", retryable=False)
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
            raise ToolProviderError(
                "invalid_request", "Document path is invalid", retryable=False
            )
    *folder_parts, name = relative.parts
    return "/".join(folder_parts), name
