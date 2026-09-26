"""同 Run の保存 Artifact を、全文応答なしで読取可能な作業 file に渡す。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Any

from skillmind.agent.artifact_append import ArtifactAppendSource
from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.agent.workspace_provider import _resolve_writable_path, _write_workspace_file


class ArtifactMaterializeProvider:
    """原 byte を再取得して安全な Run path へコピーし、復旧時も同じ ref を使う。"""

    def __init__(self, source: ArtifactAppendSource | None) -> None:
        """既存の Artifact 認可・原 byte 読取を共有する。"""
        self._source = source

    async def execute(
        self,
        context: RunToolContext,
        arguments: Mapping[str, Any],
    ) -> ProviderToolResult:
        """他 Run と撤権を拒否し、保存・モデル再生成・外部書込なしで実体化する。"""
        ref = arguments.get("artifact_ref")
        if (
            context.tool.capability != "artifact.materialize/v1"
            or not isinstance(ref, str)
            or re.fullmatch(r"art_[a-zA-Z0-9_-]{1,60}", ref) is None
        ):
            raise ToolProviderError(
                "invalid_request", "Invalid Artifact reference", retryable=False
            )
        if self._source is None:
            raise ToolProviderError(
                "unavailable", "Artifact reading is unavailable", retryable=False
            )
        try:
            await self._source.authorize(context)
            original = await self._source.read_artifact(context, ref)
            metadata = original.metadata
            if (
                metadata.run_id != context.run_id
                or metadata.project_id != context.project_id
                or metadata.artifact_ref != ref
            ):
                raise ValueError("Artifact ownership differs")
            path = f"workspace/artifacts/{ref}/{metadata.checksum[7:]}.txt"
            _, _, target = await asyncio.to_thread(_resolve_writable_path, context, path)
            await self._source.authorize(context)
            await asyncio.to_thread(
                _write_workspace_file,
                context.workspace.root,
                target,
                original.content,
            )
            await self._source.authorize(context)
        except (ValueError, LookupError, PermissionError):
            raise ToolProviderError(
                "not_found", "Run Artifact is unavailable", retryable=False
            ) from None
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "platform",
                "source_artifact_ref": ref,
                "file": {
                    "path": path,
                    "content_hash": metadata.checksum,
                    "size_bytes": metadata.size_bytes,
                    "total_lines": len(original.content.decode("utf-8").splitlines()),
                },
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="artifact-materialization",
                    source_uri=f"workspace://runs/{context.run_id}/{path}",
                    source_locator={"path": path, "source_artifact_ref": ref},
                    content_hash=metadata.checksum,
                    metadata={"scope": "run-workspace", "read_only": True},
                ),
            ),
        )
