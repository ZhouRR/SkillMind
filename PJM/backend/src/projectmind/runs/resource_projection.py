"""Run の凍結資源を、履歴を改変せず公開可能な摘要と文書清単へ投影する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import UUID

from projectmind.documents.snapshot import (
    DOCUMENT_PROVIDER,
    DOCUMENT_READ_CAPABILITY,
    DocumentSnapshot,
    DocumentSnapshotError,
    is_document_source,
    parse_document_snapshot,
    snapshot_documents,
)

DocumentSnapshotStatus = Literal["FROZEN", "LEGACY_UNAVAILABLE", "INVALID"]
SOURCE_SUMMARY_FIELDS = ("provider", "capability", "resource_kind", "access")


@dataclass(frozen=True, slots=True)
class RunDocumentSnapshot:
    """検証済みの内容と、読み取れない歴史/破損を区別する公開 read model。"""

    requirement_key: str
    status: DocumentSnapshotStatus
    snapshot: DocumentSnapshot | None


def source_summaries(sources: Mapping[str, Any]) -> dict[str, str | dict[str, str | None]]:
    """内部 scope/locator/将来 field を流出させず、旧 flat provider 表示も維持する。"""

    summaries: dict[str, str | dict[str, str | None]] = {}
    for key, source in sources.items():
        if isinstance(source, str):
            summaries[key] = source
        elif isinstance(source, Mapping):
            summaries[key] = {
                field: source[field] if isinstance(source.get(field), str) else None
                for field in SOURCE_SUMMARY_FIELDS
            }
    return summaries


def document_snapshots(
    sources: Mapping[str, Any], *, project_id: UUID
) -> tuple[RunDocumentSnapshot, ...]:
    """保存された Project/slot/hash だけを検証し、現在の文書を照会・補填しない。"""

    result: list[RunDocumentSnapshot] = []
    for key, source in sorted(sources.items()):
        if not is_document_source(source):
            continue
        if not isinstance(source, Mapping) or "document_snapshot" not in source:
            result.append(RunDocumentSnapshot(key, "LEGACY_UNAVAILABLE", None))
            continue
        value = source["document_snapshot"]
        if (
            not isinstance(value, Mapping)
            or source.get("provider") != DOCUMENT_PROVIDER
            or source.get("capability") != DOCUMENT_READ_CAPABILITY
            or source.get("resource_kind", "document") != "document"
        ):
            result.append(RunDocumentSnapshot(key, "INVALID", None))
            continue
        try:
            snapshot = parse_document_snapshot(value, project_id=project_id, requirement_key=key)
        except DocumentSnapshotError:
            # 不正 metadata の本文や例外詳細を公開しない。Result の歴史読取は継続できる。
            result.append(RunDocumentSnapshot(key, "INVALID", None))
        else:
            result.append(RunDocumentSnapshot(key, "FROZEN", snapshot))
    try:
        snapshot_documents([item.snapshot for item in result if item.snapshot is not None])
    except DocumentSnapshotError:
        # 単独では正しくても、slot 間で同一 ID/path の事実が矛盾する集合を「凍結済み」としない。
        result = [
            replace(item, status="INVALID", snapshot=None) if item.snapshot is not None else item
            for item in result
        ]
    return tuple(result)
