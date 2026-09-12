"""MarkItDown の応答と保存 Artifact を、同一 UTF-8 byte と原文 identity で結ぶ。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote
from uuid import UUID

from skillmind.artifacts.domain import ArtifactDraft


def conversion_artifact(
    response: Mapping[str, Any], *, run_id: UUID,
) -> tuple[ArtifactDraft, dict[str, Any]]:
    """Provider/監査/読取が共用する原 byte と Evidence 定位情報を導出する。"""

    document, markdown, converter = (
        response.get("document"), response.get("markdown"), response.get("converter")
    )
    if (
        not isinstance(document, Mapping) or not isinstance(markdown, str) or not markdown
        or not isinstance(converter, Mapping) or set(converter) != {"name", "version"}
        or converter.get("name") != "markitdown"
        or not isinstance(converter.get("version"), str)
        or not 1 <= len(converter["version"]) <= 64
    ):
        raise ValueError("Conversion Artifact response is invalid")
    document_id, source_hash = document.get("document_id"), document.get("checksum")
    if (
        not isinstance(document_id, str) or str(UUID(document_id)) != document_id
        or UUID(document_id).int == 0 or not isinstance(source_hash, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", source_hash) is None
    ):
        raise ValueError("Conversion Artifact source is invalid")
    # 表示 path は可変 workspace の実 file ではなく、保存 byte の論理名である。
    artifact = ArtifactDraft(
        path=f"output/document-conversions/{document_id}/source.md",
        content=markdown.encode("utf-8"),
    )
    if response.get("markdown_checksum") != artifact.checksum:
        raise ValueError("Conversion Artifact checksum does not match Markdown")
    return artifact, {
        "evidence_type": "document-conversion",
        "source_uri": f"conversion://runs/{run_id}/{quote(artifact.path, safe='/')}",
        "source_locator": {
            "document_id": document_id, "source_checksum": source_hash,
            "path": artifact.path, "bytes": len(artifact.content),
        },
        "content_hash": artifact.checksum,
        "snapshot_uri": None,
        "metadata": {"converter": dict(converter)},
    }


def conversion_artifact_description(artifact: ArtifactDraft) -> dict[str, Any]:
    """文書保存提案に必要な元 path/size/hash を、本文を再生成させず返す。"""

    return {
        "path": artifact.path, "size_bytes": len(artifact.content),
        "content_hash": artifact.checksum, "mime_type": artifact.mime_type,
    }
