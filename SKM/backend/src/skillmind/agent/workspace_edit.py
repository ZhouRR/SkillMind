"""モデルに原文を再出力させず、Run 内の確定 byte から編集・コピー・成果公開する。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from skillmind.agent.evidence import EvidenceDraft, EvidenceRecord, ToolInvocation
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.agent.workspace_provider import (
    _read_workspace_bytes,
    _resolve_workspace_path,
    _resolve_writable_path,
    _workspace_uri,
    _write_workspace_file,
)
from skillmind.artifacts.domain import MAX_ARTIFACT_BYTES, ArtifactDraft
from skillmind.core.hashing import sha256_hex

CAPABILITY = "workspace.edit/v1"


class WorkspaceEditProvider:
    """入力は不変に保ち、原 hash を確認した一つのファイルだけを変更する。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """操作は原 byte に適用し、応答には本文でなく path/hash/成果回执を返す。"""
        operation = arguments.get("operation")
        path = arguments.get("path")
        source = arguments.get("source_path", path)
        relative, target = await asyncio.to_thread(
            _resolve_workspace_path,
            context,
            source,
            require_file=True,
        )
        data = await asyncio.to_thread(
            _read_workspace_bytes,
            context,
            relative,
            target,
            max_bytes=MAX_ARTIFACT_BYTES,
        )
        original_hash = f"sha256:{sha256_hex(data)}"
        if arguments.get("expected_hash") != original_hash:
            raise ToolProviderError(
                "invalid_request", "Source file changed; read its current hash", retryable=False
            )
        try:
            text = data.decode("utf-8")
            if operation == "append":
                text += arguments["text"]
            elif operation == "edit":
                # 各置換は一意な原文字列を要求し、誤った箇所や全一致置換を推定しない。
                for edit in arguments["edits"]:
                    old, new = edit["old"], edit["new"]
                    if not old or text.count(old) != 1:
                        raise ValueError("Replacement must match exactly once")
                    text = text.replace(old, new, 1)
            elif operation not in {"copy", "publish"}:
                raise ValueError("Unknown file operation")
            updated = text.encode("utf-8")
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ToolProviderError(
                "invalid_request", "File edit does not match the original text", retryable=False
            ) from None
        if len(updated) > MAX_ARTIFACT_BYTES:
            raise ToolProviderError(
                "too_large", "Edited file exceeds the Artifact limit", retryable=False
            )
        destination, _, target = await asyncio.to_thread(_resolve_writable_path, context, path)
        if operation == "publish" and (source != path or not destination.startswith("output/")):
            raise ToolProviderError(
                "invalid_request", "Publish requires the existing output path", retryable=False
            )
        artifact = (
            ArtifactDraft(path=destination, content=updated)
            if destination.startswith("output/")
            else None
        )
        checksum = f"sha256:{sha256_hex(updated)}"
        # 必須の参照と byte を書込前に確定する。公開 DB 提交失敗を成功と扱わない。
        draft = EvidenceDraft(
            evidence_type="workspace-edit",
            source_uri=_workspace_uri(context, destination),
            source_locator={
                "path": destination,
                "source_path": relative,
                "source_hash": original_hash,
            },
            content_hash=checksum,
            metadata={"scope": "run-workspace", "operation": operation, "read_only": False},
            artifact=artifact,
        )
        created = False
        if operation != "publish":
            created = await asyncio.to_thread(
                _write_workspace_file,
                context.workspace.root,
                target,
                updated,
                expected_hash=original_hash if relative == destination else "absent",
            )
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "workspace",
                "path": destination,
                "content_hash": checksum,
                "bytes_written": len(updated),
                "created": created,
                "warnings": [],
            },
            evidence=(draft,),
        )


def validate_edit_publication(
    invocation: ToolInvocation,
    *,
    result: Mapping[str, Any],
    evidence: Sequence[EvidenceRecord],
) -> None:
    """後から可変ファイルを開かず、確定済み byte・元要求・Evidence を照合する。"""
    arguments = invocation.arguments
    path = arguments.get("path")
    if not isinstance(path, str):
        raise ValueError("File edit path is invalid")
    if (
        invocation.tool.provider != "workspace"
        or invocation.tool.integration_id is not None
        or len(evidence) != 1
    ):
        raise ValueError("File edit publication identity is invalid")
    record = evidence[0]
    draft = record.draft
    if (
        result.get("status") != "success"
        or result.get("provider") != "workspace"
        or result.get("path") != path
        or result.get("content_hash") != draft.content_hash
        or result.get("evidence_refs") != [record.evidence_ref]
        or draft.evidence_type != "workspace-edit"
        or draft.source_uri != f"workspace://runs/{invocation.run_id}/{quote(path, safe='/')}"
        or draft.snapshot_uri is not None
        or draft.source_locator
        != {
            "path": path,
            "source_path": arguments.get("source_path", path),
            "source_hash": arguments.get("expected_hash"),
        }
        or draft.metadata
        != {"scope": "run-workspace", "operation": arguments.get("operation"), "read_only": False}
    ):
        raise ValueError("File edit publication differs from its request")
    if path.startswith("output/"):
        if (
            draft.artifact is None
            or draft.artifact.path != path
            or draft.artifact.checksum != draft.content_hash
            or len(draft.artifact.content) != result.get("bytes_written")
            or not isinstance(record.artifact_ref, str)
            or re.fullmatch(r"art_[a-f0-9]{32}", record.artifact_ref) is None
            or result.get("artifact_refs") != [record.artifact_ref]
        ):
            raise ValueError("File edit Artifact does not match its bytes")
    elif (
        draft.artifact is not None
        or record.artifact_ref is not None
        or result.get("artifact_refs") != []
    ):
        raise ValueError("Intermediate edits do not publish Artifacts")
