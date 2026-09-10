"""実 Alembic graph と外部接続を持たない fake で、読取専用の migration preflight を検証する。"""

from __future__ import annotations

import asyncio
import json
import runpy
import sys
from dataclasses import dataclass
from pathlib import Path
from types import CoroutineType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import UUID

import pytest
from alembic.script import ScriptDirectory
from alembic.script.revision import Revision, RevisionMap
from alembic.util import CommandError
from redis.asyncio import Redis
from skillmind.ops import preflight

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
_PRIVATE_DIAGNOSTIC = "test-only-opaque-private-diagnostic"


@pytest.fixture
def graph() -> ScriptDirectory:
    """Migration 関数や env.py を実行せず、同梱された revision graph を読む。"""

    return ScriptDirectory(str(_MIGRATIONS))


@pytest.fixture
def settings() -> Mock:
    """Settings を構築せず、環境 file と実 credential を読まない検査用属性を渡す。"""

    return Mock(
        database_url="postgresql+asyncpg://database.invalid/preflight-test",
        redis_url="redis://redis.invalid/0",
        object_storage_namespace_id=UUID("00000000-0000-4000-8000-000000000001"),
    )


def _graph(revisions: tuple[tuple[str, str | None], ...]) -> ScriptDirectory:
    """File を生成せず、Alembic 自身の RevisionMap で空 graph と分岐を作る。"""

    script = ScriptDirectory(str(_MIGRATIONS))
    script.revision_map = RevisionMap(
        lambda: (Revision(identifier, parent) for identifier, parent in revisions)
    )
    return script


@pytest.mark.parametrize("migration_plan", [False, True])
@pytest.mark.parametrize(
    ("namespace", "reason"), [(None, "namespace_missing"), (UUID(int=0), "namespace_invalid")],
)
async def test_namespace_configuration_stops_before_external_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Mock,
    migration_plan: bool, namespace: UUID | None, reason: str,
) -> None:
    """欠落/ゼロ namespace は両入口で非成功とし、接続前に安全な修正先を示す。"""

    settings.object_storage_namespace_id = namespace
    database = AsyncMock(side_effect=AssertionError("Database must not be contacted"))
    redis = Mock(side_effect=AssertionError("Redis must not be contacted"))
    monkeypatch.setattr(preflight, "Settings", Mock(return_value=settings))
    monkeypatch.setattr(preflight, "inspect_database", database)
    monkeypatch.setattr(preflight.Redis, "from_url", redis)

    assert await preflight._main(migration_plan=migration_plan) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["status"] == "not_ready"
    check = report["checks"]["object_storage_configuration"]
    assert check["reason"] == reason
    assert check["field"] == "SKILLMIND_OBJECT_STORAGE_NAMESPACE_ID"
    assert ".env" in check["hint"] and "Preserve" in check["hint"]
    assert settings.database_url not in output and settings.redis_url not in output
    database.assert_not_called()
    redis.assert_not_called()


@pytest.mark.parametrize("allow_pending", [False, True])
def test_current_head_requires_no_migration(graph: ScriptDirectory, allow_pending: bool) -> None:
    """現在 head は readiness と計画のどちらでも pending を生成しない。"""

    head = graph.get_current_head()
    assert head is not None
    assert preflight.migration_check(graph, [head], allow_pending=allow_pending) == {
        "status": "ok", "migration_head": head, "current": [head], "pending": [],
    }


@pytest.mark.parametrize("current", [[], ["0026_frontend_module_versions"]])
def test_empty_or_old_database_can_plan_forward_but_is_not_ready(
    graph: ScriptDirectory, current: list[str],
) -> None:
    """初回 DB と中間版は計画だけを許し、前進順に列挙して readiness と区別する。"""

    head = graph.get_current_head()
    assert head is not None
    not_ready = preflight.migration_check(graph, current, allow_pending=False)
    assert not_ready == {
        "status": "error", "reason": "migration_head_mismatch",
        "expected": head, "actual": current[0] if current else None,
    }
    plan = preflight.migration_check(graph, current, allow_pending=True)
    assert plan["status"] == "ok" and plan["current"] == current
    assert plan["migration_head"] == head
    previous = current[0] if current else None
    for identifier in plan["pending"]:
        revision = graph.get_revision(identifier)
        assert revision is not None and revision.down_revision == previous
        previous = identifier
    assert previous == head
    assert plan["pending"][0] == (
        "0027_subagent_sessions" if current else graph.get_base()
    )


@pytest.mark.parametrize("allow_pending", [False, True])
@pytest.mark.parametrize(
    ("current", "reason"),
    [
        ([_PRIVATE_DIAGNOSTIC], "unknown_database_revision"),
        (["head"], "unknown_database_revision"),
        (["base"], "unknown_database_revision"),
        (["0026"], "unknown_database_revision"),
        (["0026_frontend_module_versions"] * 2, "multiple_database_revisions"),
        (["0026_frontend_module_versions", _PRIVATE_DIAGNOSTIC], "multiple_database_revisions"),
    ],
)
def test_unknown_alias_prefix_or_multiple_database_revisions_fail_closed(
    graph: ScriptDirectory, current: list[str], reason: str, allow_pending: bool,
) -> None:
    """DB の値を Alembic の略号として解釈せず、未知の原値を report に出さない。"""

    report = preflight.migration_check(graph, current, allow_pending=allow_pending)
    assert report == {"status": "error", "reason": reason}
    assert _PRIVATE_DIAGNOSTIC not in json.dumps(report)


@pytest.mark.parametrize("allow_pending", [False, True])
def test_target_graph_without_a_head_is_rejected(allow_pending: bool) -> None:
    """空の target graph は DB の状態にかかわらず成功扱いしない。"""

    assert preflight.migration_check(_graph(()), [], allow_pending=allow_pending) == {
        "status": "error", "reason": "migration_head_missing",
    }


@pytest.mark.parametrize("allow_pending", [False, True])
def test_target_graph_with_multiple_heads_is_rejected(allow_pending: bool) -> None:
    """Alembic の分岐解決を暗黙に選ばず、複数 target head を拒否する。"""

    script = _graph((("base_a", None), ("head_a", "base_a"), ("head_b", "base_a")))
    with pytest.raises(CommandError):
        preflight.migration_check(script, ["base_a"], allow_pending=allow_pending)


@dataclass
class DatabaseHarness:
    """全 DB 操作を保持し、実 engine や transaction を生成しない test port。"""

    engine: MagicMock
    connection: AsyncMock
    result: Mock
    factory: Mock


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch, graph: ScriptDirectory) -> DatabaseHarness:
    """DB の存在照会と全 revision 読取を fake に差し替える。"""

    connection = AsyncMock()
    connection.scalar.return_value = "alembic_version"
    result = Mock()
    result.scalars.return_value.all.return_value = [graph.get_current_head()]
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
    engine.connect.return_value.__aexit__ = AsyncMock(return_value=False)
    engine.dispose = AsyncMock()
    factory = Mock(return_value=engine)
    monkeypatch.setattr(preflight, "create_async_engine", factory)
    monkeypatch.setattr(ScriptDirectory, "from_config", Mock(return_value=graph))
    return DatabaseHarness(engine, connection, result, factory)


@pytest.mark.parametrize("allow_pending", [False, True])
@pytest.mark.parametrize("present", [False, True])
async def test_missing_or_empty_version_table_is_read_only_and_not_implicitly_ready(
    database: DatabaseHarness, settings: Mock, allow_pending: bool, present: bool,
) -> None:
    """版 table が無い状態も空の状態も stamp/CREATE せず、計画と通常検査を分ける。"""

    database.connection.scalar.return_value = "alembic_version" if present else None
    database.result.scalars.return_value.all.return_value = []
    report = await preflight.inspect_database(settings, allow_pending=allow_pending)
    assert report["status"] == ("ok" if allow_pending else "error")
    if allow_pending:
        assert report["current"] == [] and report["pending"]
    else:
        assert report["reason"] == "migration_head_mismatch" and report["actual"] is None
    database.factory.assert_called_once_with(settings.database_url, pool_pre_ping=True)
    assert str(database.connection.scalar.call_args.args[0]) == (
        "SELECT to_regclass('alembic_version')"
    )
    database.connection.scalar.assert_awaited_once()
    if present:
        database.connection.execute.assert_awaited_once()
        assert str(database.connection.execute.call_args.args[0]) == (
            "SELECT version_num FROM alembic_version"
        )
    else:
        database.connection.execute.assert_not_called()
    database.connection.begin.assert_not_called()
    database.connection.commit.assert_not_called()
    database.engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("allow_pending", [False, True])
async def test_inspect_database_rejects_every_extra_revision_row(
    database: DatabaseHarness, settings: Mock, allow_pending: bool,
) -> None:
    """先頭行が期待 head でも後続行を読み捨てず、複数 DB revision を拒否する。"""

    database.result.scalars.return_value.all.return_value.append(_PRIVATE_DIAGNOSTIC)
    report = await preflight.inspect_database(settings, allow_pending=allow_pending)
    assert report == {"status": "error", "reason": "multiple_database_revisions"}
    database.result.scalars.return_value.all.assert_called_once_with()
    database.engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("phase", ["create", "connect", "relation", "revision", "cleanup"])
async def test_database_failures_hide_original_diagnostic_and_attempt_cleanup(
    database: DatabaseHarness, settings: Mock, phase: str,
) -> None:
    """接続・照会・cleanup の失敗は型だけを公開し、成功と取り違えない。"""

    target = {
        "create": database.factory,
        "connect": database.engine.connect.return_value.__aenter__,
        "relation": database.connection.scalar,
        "revision": database.connection.execute,
        "cleanup": database.engine.dispose,
    }[phase]
    target.side_effect = RuntimeError(_PRIVATE_DIAGNOSTIC)
    report = await preflight.inspect_database(settings, allow_pending=True)
    assert report == {
        "status": "error", "type": "RuntimeError",
        **({"phase": "cleanup"} if phase == "cleanup" else {}),
    }
    assert _PRIVATE_DIAGNOSTIC not in json.dumps(report)
    if phase == "create":
        database.engine.dispose.assert_not_called()
    else:
        database.engine.dispose.assert_awaited_once()


async def test_target_multiple_heads_are_safely_reported_by_database_boundary(
    monkeypatch: pytest.MonkeyPatch, database: DatabaseHarness, settings: Mock,
) -> None:
    """Graph helper の拒否も公開 report では安全な infrastructure error にする。"""

    script = _graph((("base_a", None), ("head_a", "base_a"), ("head_b", "base_a")))
    monkeypatch.setattr(ScriptDirectory, "from_config", Mock(return_value=script))
    assert await preflight.inspect_database(settings, allow_pending=True) == {
        "status": "error", "type": "CommandError",
    }
    database.engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("postgres_status", ["ok", "error"])
async def test_migration_plan_never_constructs_or_contacts_redis(
    monkeypatch: pytest.MonkeyPatch, settings: Mock, postgres_status: str,
) -> None:
    """計画は DB の読取だけに限定し、成功・失敗どちらでも Redis を作成しない。"""

    postgres = {"status": postgres_status}
    inspect = AsyncMock(return_value=postgres)
    redis_factory = Mock(side_effect=AssertionError("Redis must remain untouched"))
    monkeypatch.setattr(preflight, "inspect_database", inspect)
    monkeypatch.setattr(Redis, "from_url", redis_factory)
    report = await preflight.inspect_infrastructure(settings, migration_plan=True)
    assert report == {
        "status": "ready" if postgres_status == "ok" else "not_ready",
        "checks": {"postgres": postgres},
    }
    inspect.assert_awaited_once_with(settings, allow_pending=True)
    redis_factory.assert_not_called()


@pytest.mark.parametrize("phase", ["healthy", "create", "ping", "unexpected-pong", "cleanup"])
@pytest.mark.parametrize("postgres_status", ["ok", "error"])
async def test_regular_preflight_checks_redis_and_hides_connection_errors(
    monkeypatch: pytest.MonkeyPatch, settings: Mock, phase: str, postgres_status: str,
) -> None:
    """通常 readiness は PING と cleanup を要求し、Redis の原例外を出力しない。"""

    inspect = AsyncMock(return_value={"status": postgres_status})
    redis = AsyncMock()
    redis.ping.return_value = True
    factory = Mock(return_value=redis)
    if phase == "create":
        factory.side_effect = RuntimeError(_PRIVATE_DIAGNOSTIC)
    elif phase == "ping":
        redis.ping.side_effect = RuntimeError(_PRIVATE_DIAGNOSTIC)
    elif phase == "unexpected-pong":
        redis.ping.return_value = 1
    elif phase == "cleanup":
        redis.aclose.side_effect = RuntimeError(_PRIVATE_DIAGNOSTIC)
    monkeypatch.setattr(preflight, "inspect_database", inspect)
    monkeypatch.setattr(Redis, "from_url", factory)
    report = await preflight.inspect_infrastructure(settings)
    assert report["status"] == (
        "ready" if phase == "healthy" and postgres_status == "ok" else "not_ready"
    )
    assert report["checks"]["redis"] == (
        {"status": "ok"} if phase == "healthy" else {
            "status": "error", "type": "RuntimeError",
            **({"phase": "cleanup"} if phase == "cleanup" else {}),
        }
    )
    assert _PRIVATE_DIAGNOSTIC not in json.dumps(report)
    inspect.assert_awaited_once_with(settings, allow_pending=False)
    factory.assert_called_once_with(settings.redis_url, decode_responses=True)
    if phase == "create":
        redis.ping.assert_not_called()
        redis.aclose.assert_not_called()
    else:
        redis.ping.assert_awaited_once()
        redis.aclose.assert_awaited_once()


@pytest.mark.parametrize("migration_plan", [False, True])
@pytest.mark.parametrize("ready", [False, True])
async def test_main_prints_one_json_report_and_matching_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Mock,
    migration_plan: bool, ready: bool,
) -> None:
    """CLI 本体は mode をそのまま渡し、機械判読可能な JSON と終了 code を返す。"""

    report: dict[str, Any] = {"status": "ready" if ready else "not_ready", "checks": {}}
    inspect = AsyncMock(return_value=report)
    monkeypatch.setattr(preflight, "Settings", Mock(return_value=settings))
    monkeypatch.setattr(preflight, "inspect_infrastructure", inspect)
    assert await preflight._main(migration_plan=migration_plan) == (0 if ready else 1)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == report and captured.err == ""
    inspect.assert_awaited_once_with(settings, migration_plan=migration_plan)


async def test_invalid_settings_are_secret_safe_and_do_not_start_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """設定例外の入力値や traceback を公開せず、接続の前に非零終了する。"""

    inspect = AsyncMock()
    monkeypatch.setattr(preflight, "Settings", Mock(side_effect=ValueError(_PRIVATE_DIAGNOSTIC)))
    monkeypatch.setattr(preflight, "inspect_infrastructure", inspect)
    assert await preflight._main(migration_plan=True) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "not_ready", "checks": {
        "configuration": {"status": "error", "type": "ValueError"},
    }}
    assert _PRIVATE_DIAGNOSTIC not in captured.out and captured.err == ""
    inspect.assert_not_called()


async def test_main_migration_plan_composes_read_only_database_checks_without_redis(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    database: DatabaseHarness, settings: Mock,
) -> None:
    """CLI 本体から実 graph 検査まで通し、未移行 DB の計画で Redis や DDL を呼ばない。"""

    database.connection.scalar.return_value = None
    redis_factory = Mock(side_effect=AssertionError("Redis must remain untouched"))
    monkeypatch.setattr(preflight, "Settings", Mock(return_value=settings))
    monkeypatch.setattr(Redis, "from_url", redis_factory)
    assert await preflight._main(migration_plan=True) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["status"] == "ready" and set(report["checks"]) == {"postgres"}
    postgres = report["checks"]["postgres"]
    assert postgres["status"] == "ok" and postgres["current"] == []
    assert postgres["pending"][-1] == postgres["migration_head"]
    assert captured.err == ""
    assert str(database.connection.scalar.call_args.args[0]) == (
        "SELECT to_regclass('alembic_version')"
    )
    database.connection.execute.assert_not_called()
    database.connection.commit.assert_not_called()
    database.engine.dispose.assert_awaited_once()
    redis_factory.assert_not_called()


@pytest.mark.parametrize("arguments", [[], ["--migration-plan"]])
@pytest.mark.parametrize("code", [0, 1])
def test_command_line_flag_reaches_main_and_preserves_exit_code(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], code: int,
) -> None:
    """Module 起動の argparse 配線を実行し、Settings や外部接続には進まない。"""

    seen: list[bool] = []

    def run(coroutine: CoroutineType[Any, Any, int]) -> int:
        """未実行 coroutine の引数だけ確認して閉じ、起動結果を模擬する。"""

        frame = coroutine.cr_frame
        assert frame is not None
        seen.append(frame.f_locals["migration_plan"])
        coroutine.close()
        return code

    monkeypatch.setattr(asyncio, "run", run)
    monkeypatch.setattr(sys, "argv", ["skillmind.ops.preflight", *arguments])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(Path(preflight.__file__)), run_name="__main__")
    assert raised.value.code == code
    assert seen == [bool(arguments)]
