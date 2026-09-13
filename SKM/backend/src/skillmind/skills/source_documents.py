"""解釈要約と独立して、凍結された Skill 原文を検証・伝達する。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from skillmind.core.hashing import sha256_hex
from skillmind.skills.domain import InlineSkillFile
from skillmind.skills.importer import SkillImportLimits


def build_source_documents(files: Sequence[InlineSkillFile]) -> list[dict[str, str]]:
    """検証済み text snapshot を省略・翻訳せず、原 byte の hash とともに固定する。"""

    return validate_source_documents([
        {"path": item.path, "content": item.content,
         "sha256": f"sha256:{sha256_hex(item.content.encode('utf-8'))}"}
        for item in sorted(files, key=lambda item: item.path)
    ])


def validate_source_documents(value: Any) -> list[dict[str, str]]:
    """原文の破損・重複・上限超過を拒否し、内容を含まないエラーだけを返す。"""

    limits = SkillImportLimits()
    if not isinstance(value, list) or not 1 <= len(value) <= limits.max_files:
        raise ValueError("Frozen Skill source documents are invalid")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    total = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "content", "sha256"}:
            raise ValueError("Frozen Skill source document is invalid")
        path, content, checksum = item["path"], item["content"], item["sha256"]
        if (
            not isinstance(path, str) or not 0 < len(path) <= 1024
            or not path.isprintable() or any(c in path for c in ("\\", ":"))
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in seen or not isinstance(content, str)
        ):
            raise ValueError("Frozen Skill source document is invalid")
        raw = content.encode("utf-8")
        total += len(raw)
        if (
            len(raw) > limits.max_file_bytes or total > limits.max_total_bytes
            or checksum != f"sha256:{sha256_hex(raw)}"
        ):
            raise ValueError("Frozen Skill source document integrity check failed")
        seen.add(path)
        result.append({"path": path, "content": content, "sha256": checksum})
    return result
