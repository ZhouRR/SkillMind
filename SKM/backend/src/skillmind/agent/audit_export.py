"""保存済みの監査事実を原 Run の成果へ投影し、モデルによる原値の再作成を不要にする。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote
from uuid import UUID

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.agent.workspace_provider import (
    _resolve_writable_path,
    _workspace_uri,
    _write_workspace_file,
)
from skillmind.artifacts.domain import ArtifactDraft
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.redaction import contains_sensitive_content, find_sensitive_key

AUDIT_EXPORT_CAPABILITY = "audit.export/v1"
AUDIT_EXPORT_VERSION = "skillmind.audit-export/v1"
MAX_AUDIT_BYTES = 1_048_576
MAX_AUDIT_REFS = 50

if TYPE_CHECKING:
    from skillmind.agent.evidence import EvidenceRecord, ToolInvocation


class AuditExportSource(Protocol):
    """原 Run の保存済み値と現在の参照権限だけを扱う。外部サービスを再実行しない。"""

    async def authorize(self, context: RunToolContext) -> None:
        """呼出し前後の現在の Project 読取権限を確認する。"""
        ...

    async def read(
        self,
        context: RunToolContext,
        *,
        evidence_refs: tuple[str, ...],
        proposal_refs: tuple[str, ...],
    ) -> dict[str, Any]:
        """指定された全参照を取得する。不在・別 Run は区別せず拒否する。"""
        ...


def export_selection(arguments: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """有界な原参照だけを受け付け、本文・SQL・別 Run の選択を許可しない。"""
    selections: list[tuple[str, ...]] = []
    for key, prefix in (("evidence_refs", "ev"), ("proposal_refs", "cp")):
        value = arguments.get(key, [])
        if (
            not isinstance(value, list)
            or len(value) > MAX_AUDIT_REFS
            or any(
                not isinstance(ref, str)
                or re.fullmatch(rf"{prefix}_[a-zA-Z0-9_-]{{1,60}}", ref) is None
                for ref in value
            )
            or len(set(value)) != len(value)
        ):
            raise ValueError("Invalid audit selection")
        selections.append(tuple(value))
    if not 1 <= sum(map(len, selections)) <= MAX_AUDIT_REFS:
        raise ValueError("Invalid audit selection size")
    return selections[0], selections[1]


def export_document(
    run_id: UUID, selected: tuple[tuple[str, ...], tuple[str, ...]], records: Mapping[str, Any]
) -> bytes:
    """全件を正確に含む中立 JSON を生成する。欠落を省略、推測、成功補完しない。"""
    evidence_refs, proposal_refs = selected
    evidence, proposals = records.get("evidence"), records.get("proposals")
    if not isinstance(evidence, list) or not isinstance(proposals, list):
        raise ValueError("Audit export records are invalid")
    for items, refs, key in (
        (evidence, evidence_refs, "evidence_ref"),
        (proposals, proposal_refs, "proposal_ref"),
    ):
        if len(items) != len(refs) or any(
            not isinstance(item, dict) or item.get(key) != ref
            for item, ref in zip(items, refs, strict=True)
        ):
            raise ValueError("Audit export selection does not match")
    document = {
        "audit_version": AUDIT_EXPORT_VERSION,
        "run_id": str(run_id),
        "selection": {"evidence_refs": list(evidence_refs), "proposal_refs": list(proposal_refs)},
        "evidence": deepcopy(evidence),
        "proposals": deepcopy(proposals),
        "meaning": (
            "Saved observations and effect receipts; "
            "not a business verdict or current remote state."
        ),
    }
    text = canonical_json(document) + "\n"
    # 過去の保存規則が違っても、機密らしい本文を新たな公開 Artifact へ運ばない。
    if find_sensitive_key(document) is not None or contains_sensitive_content(text):
        raise ValueError("Audit export contains restricted data")
    data = text.encode("utf-8")
    if len(data) > MAX_AUDIT_BYTES:
        raise OverflowError("Audit export exceeds its byte limit")
    return data


class AuditExportProvider:
    """既存 workspace 出力と Artifact 提交を使い、外部保存は原 Effect に委ねる。"""

    def __init__(self, source: AuditExportSource | None) -> None:
        """DB read port を注入する。未装配は Tool 使用時だけ失敗し他の仕事を止めない。"""
        self._source = source

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """正確な選択と hash を検証し、元データ本文をモデル応答へ繰り返し返さない。"""
        if self._source is None:
            raise ToolProviderError("unavailable", "Audit export is unavailable", retryable=False)
        if (
            context.tool.capability != AUDIT_EXPORT_CAPABILITY
            or context.tool.provider != "platform"
        ):
            raise ToolProviderError(
                "invalid_request", "Audit export context is invalid", retryable=False
            )
        relative, _, target = await asyncio.to_thread(
            _resolve_writable_path, context, arguments.get("path")
        )
        if not relative.startswith("output/"):
            raise ToolProviderError(
                "invalid_request", "Audit export requires output/", retryable=False
            )
        try:
            selection = export_selection(arguments)
            await self._source.authorize(context)
            records = await self._source.read(
                context, evidence_refs=selection[0], proposal_refs=selection[1]
            )
            data = export_document(context.run_id, selection, records)
            await self._source.authorize(context)
        except OverflowError:
            raise ToolProviderError(
                "too_large", "Select fewer audit references", retryable=False
            ) from None
        except (ValueError, LookupError, PermissionError):
            raise ToolProviderError(
                "not_found", "Audit selection is unavailable", retryable=False
            ) from None
        checksum = "sha256:" + sha256_hex(data)
        evidence = EvidenceDraft(
            evidence_type="audit-export",
            source_uri=_workspace_uri(context, relative),
            source_locator={"path": relative, "bytes": len(data)},
            content_hash=checksum,
            metadata={
                "audit_version": AUDIT_EXPORT_VERSION,
                "selection": {
                    "evidence_refs": list(selection[0]),
                    "proposal_refs": list(selection[1]),
                },
            },
            excerpt=None,
            artifact=ArtifactDraft(path=relative, content=data),
        )
        created = await asyncio.to_thread(
            _write_workspace_file, context.workspace.root, target, data
        )
        try:
            await self._source.authorize(context)
        except (PermissionError, LookupError):
            # ローカル出力は rollback しない。現在権限のない Artifact/本文は公開しない。
            raise ToolProviderError(
                "not_found", "Audit selection is unavailable", retryable=False
            ) from None
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "platform",
                "path": relative,
                "content_hash": checksum,
                "bytes_written": len(data),
                "created": created,
                "counts": {"evidence": len(selection[0]), "proposals": len(selection[1])},
            },
            evidence=(evidence,),
        )


def validate_export_artifact(
    invocation: ToolInvocation,
    *,
    result: Mapping[str, Any],
    evidence: tuple[EvidenceRecord, ...],
) -> None:
    """原選択、正確な出力 byte と一個の発行回执を照合し、モデルの自己申告を採用しない。"""
    if (
        len(evidence) != 1
        or invocation.tool.provider != "platform"
        or invocation.tool.capability != AUDIT_EXPORT_CAPABILITY
        or invocation.tool.integration_id is not None
    ):
        raise ValueError("Invalid export publication")
    record = evidence[0]
    draft = record.draft
    artifact = draft.artifact
    path = invocation.arguments.get("path")
    selected = export_selection(invocation.arguments)
    selection = {"evidence_refs": list(selected[0]), "proposal_refs": list(selected[1])}
    if artifact is None or not isinstance(path, str) or not path.startswith("output/"):
        raise ValueError("Invalid export Artifact")
    payload = json.loads(artifact.content)
    if (
        not isinstance(payload, dict)
        or payload.get("run_id") != str(invocation.run_id)
        or payload.get("audit_version") != AUDIT_EXPORT_VERSION
        or payload.get("selection") != selection
        or export_document(invocation.run_id, selected, payload) != artifact.content
        or artifact.path != path
        or artifact.mime_type != "text/plain"
        or draft.evidence_type != "audit-export"
        or draft.snapshot_uri is not None
        or draft.excerpt is not None
        or draft.source_uri != f"workspace://runs/{invocation.run_id}/{quote(path, safe='/')}"
        or result.get("status") != "success"
        or type(result.get("bytes_written")) is not int
        or draft.source_locator != {"path": path, "bytes": len(artifact.content)}
        or draft.content_hash != "sha256:" + sha256_hex(artifact.content)
        or result.get("path") != path
        or result.get("provider") != "platform"
        or result.get("content_hash") != draft.content_hash
        or result.get("bytes_written") != len(artifact.content)
        or result.get("counts") != {"evidence": len(selected[0]), "proposals": len(selected[1])}
        or draft.metadata != {"audit_version": AUDIT_EXPORT_VERSION, "selection": selection}
        or result.get("evidence_refs") != [record.evidence_ref]
        or not isinstance(record.artifact_ref, str)
        or re.fullmatch(r"art_[a-zA-Z0-9_-]{1,60}", record.artifact_ref) is None
        or result.get("artifact_refs") != [record.artifact_ref]
    ):
        raise ValueError("Export Artifact differs from its original selection")
