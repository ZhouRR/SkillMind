"""Driver ごとの constraint metadata を、DB 本文を公開せず同じ入口で判別する。"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError


def matches_constraint(error: IntegrityError, name: str) -> bool:
    """既知の constraint 名だけを識別し、その他の障害を業務競合へ誤変換しない。"""

    original = error.orig
    return any(
        getattr(item, "constraint_name", None) == name for item in (
            original, getattr(original, "__cause__", None), getattr(original, "diag", None),
        )
    )
