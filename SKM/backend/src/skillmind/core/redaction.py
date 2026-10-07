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


def find_sensitive_key(value: Any, *, include_password: bool = True) -> str | None:
    """資格の宣言を検査する。工具の応答では password 名による拒否を行わない。"""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower()
            if (
                (
                    normalized in SENSITIVE_KEY_NAMES
                    and (include_password or normalized != "password")
                )
                or any(
                    fragment in normalized
                    for fragment in _SENSITIVE_KEY_FRAGMENTS
                    if include_password or fragment != "password"
                )
            ) and not _control_identifier(key, nested):
                return str(key)
            found = find_sensitive_key(nested, include_password=include_password)
            if found is not None:
                return found
    elif isinstance(value, list | tuple):
        for nested in value:
            found = find_sensitive_key(nested, include_password=include_password)
            if found is not None:
                return found
    return None


def _control_identifier(key: Any, value: Any) -> bool:
    """UI Automation のコントロール参照を秘密値と混同せず、入れ子の資格は除外しない。"""

    normalized = str(key).lower().replace("_", "").replace("-", "")
    return (
        normalized.endswith("automationid")
        and isinstance(value, str)
        and 0 < len(value) <= 512
        and not any(ord(character) < 32 for character in value)
        and not contains_sensitive_content(value)
    )
