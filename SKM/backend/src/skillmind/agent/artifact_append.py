"""原 Run の Artifact byte に短い説明だけを追加し、同じ出力 path を更新する。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.agent.workspace_provider import _resolve_writable_path, _write_workspace_file
from skillmind.artifacts.domain import MAX_ARTIFACT_BYTES, ArtifactContent, ArtifactDraft
from skillmind.core.hashing import sha256_hex

if TYPE_CHECKING:
    from skillmind.agent.evidence import EvidenceRecord, ToolInvocation

CAPABILITY = "artifact.append/v1"


class ArtifactAppendSource(Protocol):
    """現在権限と同 Run の検証済み保存 byte を提供する。"""

    async def authorize(self, context: RunToolContext) -> None:
        """処理前後の現在権限を確認する。"""
        ...

    async def read_artifact(self, context: RunToolContext, artifact_ref: str) -> ArtifactContent:
        """他 Run・不存在・破損を拒否し、原内容を返す。"""
        ...


class ArtifactAppendProvider:
    """全文再生成や外部書込なしで、検証済み原文と付記を一つの成果にする。"""

    def __init__(self, source: ArtifactAppendSource | None) -> None:
        """Worker の認可済み保存読取を注入する。"""
        self._source = source

    async def execute(
        self,
        context: RunToolContext,
        arguments: Mapping[str, Any],
    ) -> ProviderToolResult:
        """指定 ref の byte を基準に連結し、同要求の再試行で二重追加しない。"""
        if self._source is None:
            raise ToolProviderError(
                "unavailable", "Artifact append is unavailable", retryable=False
            )
        ref, suffix = arguments.get("artifact_ref"), arguments.get("text")
        if (
            context.tool.capability != CAPABILITY
            or context.tool.provider != "platform"
            or not isinstance(ref, str)
            or re.fullmatch(r"art_[a-zA-Z0-9_-]{1,60}", ref) is None
            or not isinstance(suffix, str)
            or not 1 <= len(suffix) <= 16384
        ):
            raise ToolProviderError("invalid_request", "Invalid append request", retryable=False)
        try:
            added = suffix.encode("utf-8")
        except UnicodeError:
            raise ToolProviderError(
                "invalid_request",
                "Append text must be UTF-8",
                retryable=False,
            ) from None
        try:
            await self._source.authorize(context)
            original = await self._source.read_artifact(context, ref)
            if (
                original.metadata.run_id != context.run_id
                or original.metadata.project_id != context.project_id
                or original.metadata.artifact_ref != ref
            ):
                raise ValueError("Artifact ownership differs")
            data = original.content + added
            if len(data) > MAX_ARTIFACT_BYTES:
                raise ToolProviderError(
                    "too_large", "Combined Artifact exceeds limit", retryable=False
                )
            path, _, target = await asyncio.to_thread(
                _resolve_writable_path,
                context,
                original.metadata.path,
            )
            artifact = ArtifactDraft(path=path, content=data)
            await self._source.authorize(context)
            created = await asyncio.to_thread(
                _write_workspace_file,
                context.workspace.root,
                target,
                data,
            )
            await self._source.authorize(context)
        except (ValueError, LookupError, PermissionError):
            raise ToolProviderError(
                "not_found", "Run Artifact is unavailable", retryable=False
            ) from None
        # 可変 file は上書きするが、旧回执の ref/hash は改変しない。
        proof = {
            "source_artifact_ref": ref,
            "source_content_hash": original.metadata.checksum,
            "source_bytes": len(original.content),
            "appended_bytes": len(added),
            "append_hash": "sha256:" + sha256_hex(added),
        }
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "platform",
                "path": path,
                "content_hash": artifact.checksum,
                "bytes_written": len(data),
                "created": created,
                **proof,
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="artifact-append",
                    source_uri=f"workspace://runs/{context.run_id}/{quote(path, safe='/')}",
                    source_locator={"path": path, "bytes": len(data)},
                    content_hash=artifact.checksum,
                    metadata=proof,
                    excerpt=None,
                    artifact=artifact,
                ),
            ),
        )


def validate_append_proof(proof: Mapping[str, Any], total: int) -> None:
    """本文を取得しない一覧でも、原参照・境界・hash の保存形を検証する。"""
    size, added = proof.get("source_bytes"), proof.get("appended_bytes")
    if (
        set(proof)
        != {
            "source_artifact_ref",
            "source_content_hash",
            "source_bytes",
            "appended_bytes",
            "append_hash",
        }
        or not isinstance(proof.get("source_artifact_ref"), str)
        or re.fullmatch(r"art_[a-zA-Z0-9_-]{1,60}", proof["source_artifact_ref"]) is None
        or any(
            not isinstance(proof.get(key), str)
            or re.fullmatch(r"sha256:[a-f0-9]{64}", proof[key]) is None
            for key in ("source_content_hash", "append_hash")
        )
        or type(size) is not int
        or not 0 <= size <= MAX_ARTIFACT_BYTES
        or type(added) is not int
        or not 1 <= added <= 65536
        or type(total) is not int
        or size + added != total
        or total > MAX_ARTIFACT_BYTES
    ):
        raise ValueError("Invalid append proof")


def validate_append_bytes(data: bytes, proof: Mapping[str, Any]) -> None:
    """原文と付記の境界/hash を照合する。改行の補正・削除は行わない。"""
    validate_append_proof(proof, len(data))
    size = proof["source_bytes"]
    if proof.get("source_content_hash") != "sha256:" + sha256_hex(data[:size]) or proof.get(
        "append_hash"
    ) != "sha256:" + sha256_hex(data[size:]):
        raise ValueError("Appended Artifact bytes differ")


def validate_append_publication(
    invocation: ToolInvocation,
    *,
    result: Mapping[str, Any],
    evidence: tuple[EvidenceRecord, ...],
) -> None:
    """原要求の付記と公開 byte/回执を照合し、偽の成功・異なる原文を拒否する。"""
    if (
        len(evidence) != 1
        or invocation.tool.provider != "platform"
        or invocation.tool.integration_id is not None
    ):
        raise ValueError("Invalid append publication")
    record = evidence[0]
    draft, artifact = record.draft, record.draft.artifact
    if artifact is None:
        raise ValueError("Missing appended Artifact")
    proof = draft.metadata or {}
    text = invocation.arguments.get("text")
    if not isinstance(text, str):
        raise ValueError("Invalid append text")
    added = text.encode("utf-8")
    validate_append_bytes(artifact.content, proof)
    if (
        proof.get("source_artifact_ref") != invocation.arguments.get("artifact_ref")
        or proof.get("appended_bytes") != len(added)
        or artifact.content[proof["source_bytes"] :] != added
        or draft.evidence_type != "artifact-append"
        or draft.snapshot_uri is not None
        or draft.content_hash != artifact.checksum
        or draft.source_uri
        != f"workspace://runs/{invocation.run_id}/{quote(artifact.path, safe='/')}"
        or draft.source_locator != {"path": artifact.path, "bytes": len(artifact.content)}
        or result.get("status") != "success"
        or result.get("provider") != "platform"
        or result.get("path") != artifact.path
        or result.get("content_hash") != artifact.checksum
        or result.get("bytes_written") != len(artifact.content)
        or any(result.get(key) != value for key, value in proof.items())
        or result.get("evidence_refs") != [record.evidence_ref]
        or not isinstance(record.artifact_ref, str)
        or re.fullmatch(r"art_[a-f0-9]{32}", record.artifact_ref) is None
        or result.get("artifact_refs") != [record.artifact_ref]
    ):
        raise ValueError("Append publication differs from request")
