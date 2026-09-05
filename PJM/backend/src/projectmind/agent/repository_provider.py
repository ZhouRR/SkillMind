"""凍結 binding の scope 内だけを読む repository.read/v1 の実 Provider (計画 §19 W4)。

物化済み `input/` は tree 全体の発見用、本 Provider は「path と revision が既に分かっている
1 file の精読」用であり、両者は同じ binding・同じ scope 判定 (`repository_source`) を共有する。
Agent は URI も凭据も見ず、scope 外の path は Provider 到達前に拒否される。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from projectmind.agent.evidence import EvidenceDraft
from projectmind.agent.repository_client import RepositoryClientError
from projectmind.agent.repository_source import (
    RepositoryBindingRef,
    RepositorySnapshotSource,
)
from projectmind.agent.text_window import select_line_window
from projectmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolProviderError,
)
from projectmind.core.hashing import sha256_hex

# 1 file の読取上限。workspace.read/v1 と揃え、「物化経由なら読めるのに live では読めない」
# という説明のつかない差を作らない。
_MAX_FILE_BYTES = 1_048_576


class RepositoryReadProvider:
    """Run に凍結された repository binding から 1 file を読む read-only Provider。"""

    def __init__(self, source: RepositorySnapshotSource) -> None:
        """Binding 解決と凭据解決を担う snapshot source を注入する。"""

        self._source = source

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """Scope 内の file を指定 revision で読み、再現可能な Evidence を返す。"""

        integration_id = context.tool.integration_id
        binding_id = context.tool.binding_id
        revision = arguments.get("revision")
        path = arguments.get("path")
        if integration_id is None or binding_id is None:
            raise ToolProviderError(
                "invalid_request", "Repository binding is invalid", retryable=False
            )
        if not isinstance(revision, str) or not isinstance(path, str):
            raise ToolProviderError(
                "invalid_request", "Repository request is invalid", retryable=False
            )
        binding = RepositoryBindingRef(
            provider=context.tool.provider,
            integration_id=integration_id,
            binding_id=binding_id,
        )
        try:
            async with self._source.open(
                project_id=context.project_id,
                run_id=context.run_id,
                binding=binding,
                requested_revision=revision,
            ) as session:
                raw = await session.read_file(path, max_bytes=_MAX_FILE_BYTES)
                resolved_revision = session.revision
                provider = session.provider
        except RepositoryClientError as error:
            raise ToolProviderError(
                error.code, error.message, retryable=error.retryable
            ) from error
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolProviderError(
                "invalid_request", "Repository file is not UTF-8 text", retryable=False
            ) from error
        # 空 file を不正範囲として拒否する既存 repository.read 契約を保つ。
        window = select_line_window(content, arguments, subject="Repository", allow_empty=False)
        content_hash = f"sha256:{sha256_hex(raw)}"
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": provider,
                "revision": resolved_revision,
                "path": path,
                "content": window.content,
                "content_hash": content_hash,
                "line_start": window.line_start,
                "line_end": window.line_end,
                "warnings": ["Content was truncated"] if window.truncated else [],
                "truncated": window.truncated,
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="source_code",
                    # 凭据も接続 URI も含めない論理 URI。Integration 単位で再現先を特定できる。
                    source_uri=(
                        f"{provider}://integration/{integration_id}/{resolved_revision}/{path}"
                    ),
                    source_locator={
                        "integration_id": str(integration_id),
                        "revision": resolved_revision,
                        "path": path,
                        "line_start": window.line_start,
                        "line_end": window.line_end,
                    },
                    content_hash=content_hash,
                    excerpt=window.content[:2_000],
                    metadata={"provider": provider, "reproducibility": "frozen_revision"},
                ),
            ),
        )
