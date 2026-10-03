"""旧 Evidence の表示文脈を同一 Run の保存済み読取応答から有界に投影する。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from skillmind.core.markdown_excerpt import MAX_CONTEXT_CHARACTERS, markdown_table_preview
from skillmind.db.models import Evidence, ToolCall


def evidence_preview_metadata(
    evidence: Sequence[Evidence],
    calls: Sequence[ToolCall],
) -> dict[UUID, dict[str, Any]]:
    """DB の原値を書換えず、連続・同 hash の原応答で確認できた表だけ補う。"""

    groups: dict[tuple[UUID, str, str], list[tuple[int, str]]] = {}
    call_by_id = {call.id: call for call in calls}
    for call in calls:
        result = call.result_json
        if (
            call.status != "SUCCEEDED"
            or call.capability_version != "workspace.read/v1"
            or call.provider != "workspace"
            or not isinstance(result, dict)
            or result.get("status") != "success"
        ):
            continue
        path, checksum, content = (
            result.get("path"),
            result.get("content_hash"),
            result.get("content"),
        )
        offset = result.get("offset", 0 if result.get("line_start") == 1 else None)
        if (
            not isinstance(path, str)
            or not path.lower().endswith((".md", ".markdown"))
            or not isinstance(checksum, str)
            or not isinstance(content, str)
            or type(offset) is not int
            or not 0 <= offset < MAX_CONTEXT_CHARACTERS
        ):
            continue
        key = (call.run_id, path, checksum)
        groups.setdefault(key, []).append((offset, content[: MAX_CONTEXT_CHARACTERS - offset]))
    prefixes: dict[tuple[UUID, str, str], str] = {}
    projected: dict[UUID, dict[str, Any]] = {}
    for item in evidence:
        if (
            item.evidence_type != "workspace-file"
            or not item.excerpt
            or "excerpt_preview" in item.metadata_json
        ):
            continue
        original = call_by_id.get(item.tool_call_id)
        if (
            original is None
            or original.run_id != item.run_id
            or original.status != "SUCCEEDED"
            or original.capability_version != "workspace.read/v1"
            or original.provider != "workspace"
        ):
            continue
        result = original.result_json or {}
        offset = item.source_locator.get("offset")
        path = item.source_locator.get("path")
        if (
            result.get("status") != "success"
            or result.get("provider") != "workspace"
            or type(offset) is not int
            or offset != result.get("offset")
            or path != result.get("path")
            or item.content_hash != result.get("content_hash")
            or not isinstance(result.get("content"), str)
            or not result["content"].startswith(item.excerpt)
        ):
            continue
        key = (item.run_id, str(path), item.content_hash)
        if key not in prefixes:
            prefixes[key] = _continuous_prefix(groups.get(key, []))
        prefix = prefixes[key]
        end = offset + len(item.excerpt)
        if prefix[offset:end] != item.excerpt:
            continue
        preview = markdown_table_preview(prefix, offset, end, complete=False)
        if preview is not None:
            projected[item.id] = {**item.metadata_json, "excerpt_preview": preview}
    return projected


def _continuous_prefix(chunks: list[tuple[int, str]]) -> str:
    """欠落や不整合の応答を繋がず、最初から実際に観測済みの prefix に限る。"""

    prefix = ""
    for offset, content in sorted(chunks, key=lambda chunk: chunk[0]):
        if offset > len(prefix):
            break
        overlap = min(len(content), len(prefix) - offset)
        if prefix[offset : offset + overlap] != content[:overlap]:
            return ""
        prefix += content[overlap:]
    return prefix
