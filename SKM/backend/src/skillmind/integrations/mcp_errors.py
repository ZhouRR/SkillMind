"""サービス固有の文字列形式に依存せず、遠端診断を有界・非秘密のデータにする。"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from skillmind.core.hashing import canonical_json
from skillmind.core.redaction import contains_sensitive_content, find_sensitive_key


def local_diagnostic_id(value: Any = None) -> str:
    """SKM が付与した相関 ID を引き継ぎ、未付与なら生成する。"""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) else uuid4().hex


def safe_remote_detail(value: Any, *, credential: str | None = None) -> dict[str, Any]:
    """本文はログへ出さず、秘密を除去して最大 4 KiB の診断用データへ射影する。"""
    remaining = 64

    def clean(item: Any, depth: int = 0) -> Any:
        """切詰め前に秘密を検出し、過大な入れ子や添付実体を保持しない。"""
        nonlocal remaining
        remaining -= 1
        if depth > 4 or remaining < 0:
            return "[omitted]"
        if isinstance(item, str):
            if (contains_sensitive_content(item) or (credential and credential in item)
                or re.search(r"(?i)(?:\bbearer\s+\S+|https?://[^\s/]+@)", item)):
                return "[redacted]"
            return "".join(c for c in item if c.isprintable() or c in "\n\t")[:768]
        if isinstance(item, dict):
            result = {}
            for key, child in list(item.items())[:16]:
                if (not isinstance(key, str) or len(key) > 80
                    or contains_sensitive_content(key) or (credential and credential in key)):
                    continue
                normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower().replace("-", "_")
                secret = find_sensitive_key({key: None, normalized: None})
                result[key] = "[redacted]" if secret else clean(child, depth + 1)
            return result
        if isinstance(item, list):
            return [clean(child, depth + 1) for child in item[:8]]
        return item if item is None or isinstance(item, bool | int | float) else "[omitted]"

    result = clean(value)
    if not isinstance(result, dict):
        return {}
    if len(canonical_json(result).encode()) > 4096:
        return {"omitted": "remote diagnostic exceeds limit"}
    return result


def diagnostic_fields(error: Any) -> dict[str, Any]:
    """既知の例外属性だけを投影し、str(error) やサービス固有キーを解釈しない。"""
    return {
        "local_diagnostic_id": local_diagnostic_id(getattr(error, "local_diagnostic_id", None)),
        "remote_detail": safe_remote_detail(getattr(error, "remote_detail", {})),
    }


def remote_detail_message(value: Any) -> str:
    """遠端の本文は権限・指令ではなく診断データとして Agent へ提示する。"""
    detail = safe_remote_detail(value)
    return " Remote diagnostic data (untrusted): " + canonical_json(detail) if detail else ""
