"""PostgreSQL migration head と Redis 接続を secret-safe に事前検査する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from skillmind.core.settings import Settings


def expected_migration_head(config_path: Path = Path("alembic.ini")) -> str:
    """Image に同梱された Alembic graph から単一 head を取得する。"""

    config = Config(str(config_path))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("Alembic migration head is missing")
    return head


def migration_check(
    script: ScriptDirectory, current: list[str], *, allow_pending: bool
) -> dict[str, Any]:
    """未知 revision や複数 current を拒否し、同じ単一鎖の前進だけを認める。"""

    head = script.get_current_head()
    if head is None:
        return {"status": "error", "reason": "migration_head_missing"}
    if len(current) > 1:
        return {"status": "error", "reason": "multiple_database_revisions"}
    known = {revision.revision for revision in script.walk_revisions()}
    if any(revision not in known for revision in current):
        # 不明値をそのまま log に反映せず、正規 graph の revision だけ公開する。
        return {"status": "error", "reason": "unknown_database_revision"}
    if not allow_pending and current != [head]:
        return {
            "status": "error",
            "reason": "migration_head_mismatch",
            "expected": head,
            "actual": current[0] if current else None,
        }
    pending = list(script.iterate_revisions(head, current[0] if current else "base"))
    return {
        "status": "ok",
        "migration_head": head,
        "current": current,
        "pending": [revision.revision for revision in reversed(pending)],
    }


async def inspect_database(settings: Settings, *, allow_pending: bool = False) -> dict[str, Any]:
    """DB を変更せず全 revision 行を照合し、例外の接続情報は出力しない。"""

    engine = None
    check: dict[str, Any]
    try:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        async with engine.connect() as connection:
            present = await connection.scalar(text("SELECT to_regclass('alembic_version')"))
            current = []
            if present is not None:
                result = await connection.execute(text("SELECT version_num FROM alembic_version"))
                current = list(result.scalars().all())
        check = migration_check(script, current, allow_pending=allow_pending)
    except Exception as error:  # Infrastructure 固有例外は型名だけに正規化する。
        check = {"status": "error", "type": type(error).__name__}
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as error:  # cleanup 失敗も成功や設定不備へ取り違えない。
                check = {"status": "error", "type": type(error).__name__, "phase": "cleanup"}
    return check


async def inspect_infrastructure(
    settings: Settings, *, migration_plan: bool = False
) -> dict[str, Any]:
    """Credential や URL を出力せず DB/Redis の必須状態を返す。"""

    postgres = await inspect_database(settings, allow_pending=migration_plan)
    report: dict[str, Any] = {
        "status": "ready" if postgres["status"] == "ok" else "not_ready",
        "checks": {"postgres": postgres},
    }
    if migration_plan:
        return report
    checks = report["checks"]

    redis = None
    try:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        pong = await redis.ping()
        if pong is not True:
            raise RuntimeError("Redis ping returned an unexpected response")
        checks["redis"] = {"status": "ok"}
    except Exception as error:  # Connection detail や credential を report に含めない。
        checks["redis"] = {"status": "error", "type": type(error).__name__}
        report["status"] = "not_ready"
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception as error:  # Redis の cleanup 例外にも接続先情報を含めない。
                checks["redis"] = {
                    "status": "error",
                    "type": type(error).__name__,
                    "phase": "cleanup",
                }
                report["status"] = "not_ready"
    return report


def exit_code(report: dict[str, Any]) -> int:
    """Automation が利用できるよう readiness を process exit code へ変換する。"""

    return 0 if report.get("status") == "ready" else 1


async def _main(*, migration_plan: bool = False) -> int:
    """Environment 設定で preflight を実行し、JSON を stdout へ出力する。"""

    try:
        report = await inspect_infrastructure(Settings(), migration_plan=migration_plan)
    except Exception as error:  # Settings 検証にも Secret 原値が含まれ得るため境界で正規化する。
        report = {
            "status": "not_ready",
            "checks": {
                "configuration": {
                    "status": "error",
                    "type": type(error).__name__,
                }
            },
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return exit_code(report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migration-plan",
        action="store_true",
        help="read the migration graph/current revisions without upgrading or checking Redis",
    )
    raise SystemExit(asyncio.run(_main(migration_plan=parser.parse_args().migration_plan)))
