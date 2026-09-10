"""Container orchestration が利用する health check endpoint を提供する。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(prefix="/internal/health", include_in_schema=False)


@router.get("/live")
async def live() -> dict[str, str]:
    """Process が request を処理できることだけを返す。"""

    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """必須 infrastructure への接続を確認し、traffic 受付可否を返す。"""

    checks: dict[str, Any] = {}
    ready_status = True

    # Readiness では依存先ごとの障害を分離し、運用者が原因を判断できるようにする。
    try:
        async with request.app.state.database_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # 境界 endpoint のため、接続例外を依存先の状態へ正規化する。
        checks["postgres"] = {"status": "error", "type": type(exc).__name__}
        ready_status = False

    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # Redis client 固有例外を外部契約へ漏らさない。
        checks["redis"] = {"status": "error", "type": type(exc).__name__}
        ready_status = False

    return JSONResponse(
        {"status": "ready" if ready_status else "not_ready", "checks": checks},
        status_code=200 if ready_status else 503,
    )
