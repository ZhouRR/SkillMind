"""Credential らしい field 名と本文内容の検出を単一の判定に集約する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEY_NAMES = frozenset(
    {"api_key", "auth", "authorization", "credential", "password", "secret", "token"}
)

_SENSITIVE_KEY_FRAGMENTS = ("password", "secret", "token")

# アップロード本文や Skill source から credential 代入と private key を検出する。
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization|credential|password|secret|token)\b\s*[:=]"
)
_PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")


def contains_sensitive_content(text: str) -> bool:
    """本文に credential 代入か private key marker が含まれるかを判定する。"""

    return bool(_CREDENTIAL_ASSIGNMENT.search(text) or _PRIVATE_KEY_MARKER.search(text))


def find_sensitive_key(value: Any) -> str | None:
    """入れ子構造から最初の credential らしい key 名を返し、なければ None を返す。"""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower()
            if normalized in SENSITIVE_KEY_NAMES or any(
                fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS
            ):
                return str(key)
            found = find_sensitive_key(nested)
            if found is not None:
                return found
    elif isinstance(value, list | tuple):
        for nested in value:
            found = find_sensitive_key(nested)
            if found is not None:
                return found
    return None
