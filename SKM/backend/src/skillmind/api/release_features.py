"""認可済み API から後置機能へ入る前に、配備上限を一箇所で検証する。"""

from __future__ import annotations

from fastapi import Request

from skillmind.api.problems import ProblemException
from skillmind.core.settings import Settings


def require_deferred_features(request: Request) -> None:
    """actor/Project 検証後に呼び、履歴の存在や内容を調べず mutation を拒否する。"""
    settings: Settings = request.app.state.settings
    if not settings.deferred_features_enabled:
        raise ProblemException(
            status=409,
            title="Feature is not enabled",
            detail="This execution feature is not enabled in this deployment.",
            code="feature_not_enabled",
            headers={"Cache-Control": "no-store"},
        )
