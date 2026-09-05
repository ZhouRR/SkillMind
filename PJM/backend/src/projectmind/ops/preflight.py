"""PostgreSQL migration head と Redis 接続を secret-safe に事前検査する。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from projectmind.core.settings import Settings


def expected_migration_head(config_path: Path = Path("alembic.ini")) -> str:
    """Image に同梱された Alembic graph から単一 head を取得する。"""

    config = Config(str(config_path))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("Alembic migration head is missing")
    return head


async def inspect_infrastructure(settings: Settings) -> dict[str, Any]:
    """Credential や URL を出力せず DB/Redis の必須状態を返す。"""

    report: dict[str, Any] = {"status": "ready", "checks": {}}
    checks: dict[str, Any] = report["checks"]
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        expected_head = expected_migration_head()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            database_head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        if database_head != expected_head:
            checks["postgres"] = {
                "status": "error",
                "reason": "migration_head_mismatch",
                "expected": expected_head,
                "actual": database_head,
            }
            report["status"] = "not_ready"
        else:
            checks["postgres"] = {"status": "ok", "migration_head": expected_head}
    except Exception as error:  # Infrastructure 固有例外は型名だけに正規化する。
        checks["postgres"] = {"status": "error", "type": type(error).__name__}
        report["status"] = "not_ready"
    finally:
        await engine.dispose()

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        pong = await redis.ping()
        if pong is not True:
            raise RuntimeError("Redis ping returned an unexpected response")
        checks["redis"] = {"status": "ok"}
    except Exception as error:  # Connection detail や credential を report に含めない。
        checks["redis"] = {"status": "error", "type": type(error).__name__}
        report["status"] = "not_ready"
    finally:
        await redis.aclose()
    return report


def exit_code(report: dict[str, Any]) -> int:
    """Automation が利用できるよう readiness を process exit code へ変換する。"""

    return 0 if report.get("status") == "ready" else 1


async def _main() -> int:
    """Environment 設定で preflight を実行し、JSON を stdout へ出力する。"""

    report = await inspect_infrastructure(Settings())
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
