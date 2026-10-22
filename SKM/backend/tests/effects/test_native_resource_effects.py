"""HTTP/SQL の原要求、承認、回読と未知結果の非再送を確認する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skillmind.agent.http_source import HttpResourceError, HttpResponse
from skillmind.effects.domain import EffectLeaseValidationError
from skillmind.effects.http_provider import HttpWriteProvider
from skillmind.effects.postgres_native import (
    NativeDatabaseWriteProvider,
    SqlExecutionError,
)
from skillmind.effects.redmine import EffectProviderTransportError
from tests.agent.test_mcp_tools import execution


def http_execution():
    """事前 GET を持たない承認済み HTTP request を固定する。"""
    return replace(
        execution(),
        provider="http",
        capability_version="http.write/v1",
        operation="POST",
        target={"locator": "/messages", "display": "send message"},
        integration_config={"auth_mode": "none"},
        integration_scope={"base_urls": ["https://api.example.test"]},
        changes=(
            {
                "path": "/request",
                "action": "SET",
                "value": {
                    "request": {
                        "method": "POST",
                        "path": "/messages",
                        "body": {"message": "hello"},
                    },
                },
            },
        ),
        precondition={},
        verification={"method": "HTTP_RESPONSE", "paths": ["/request"]},
    )


def http_provider(tmp_path, source, authorize=None):
    """実 file 保存を含む Provider を隔離 workspace で作る。"""
    from skillmind.agent.workspace import WorkspaceManager

    return HttpWriteProvider(
        source=source,
        authorize=authorize or AsyncMock(),
        workspace_manager=WorkspaceManager(tmp_path),
    )


@pytest.mark.parametrize("status", [200, 201, 202, 204])
async def test_http_write_returns_exact_response_without_get_or_replay(tmp_path, status):
    """受理を含む 2xx は原応答を保存し、空 body でも架空 GET を加えない。"""
    body = b"" if status == 204 else b'{"accepted":true}'
    source = SimpleNamespace(request=AsyncMock(return_value=HttpResponse(status, {}, body)))
    authorize = AsyncMock()
    provider = http_provider(tmp_path, source, authorize)
    original = http_execution()
    result = await provider.apply(original, credential=None)
    assert result.verification["method"] == "HTTP_RESPONSE"
    assert result.verification["http_status"] == status
    assert result.verification["business_verdict"] == "NOT_EVALUATED"
    assert (
        tmp_path / str(original.run_id) / result.after.content["file"]["path"]
    ).read_bytes() == body
    assert source.request.await_count == 1 and authorize.await_count == 3
    assert source.request.await_args.kwargs["write"] is True
    with pytest.raises(EffectProviderTransportError):
        await provider.apply(replace(original, attempt_no=2), credential=None)
    assert source.request.await_count == 1


async def test_http_timeout_never_repeats_mutation(tmp_path):
    """送信済みかもしれない要求を GET や mutation で探らない。"""
    source = SimpleNamespace(
        request=AsyncMock(side_effect=HttpResourceError("transport_unconfirmed"))
    )
    with pytest.raises(EffectProviderTransportError, match="http_result_unconfirmed"):
        await http_provider(tmp_path, source).apply(http_execution(), credential=None)
    assert source.request.await_count == 1


@pytest.mark.parametrize("status", [302, 400, 429, 500])
async def test_http_error_keeps_response_and_is_not_an_unknown_result(tmp_path, status):
    """非 2xx でも正文を Agent に残し、失敗を未送信や不明へ変換しない。"""
    from skillmind.effects.http_provider import HttpResponseError
    from skillmind.effects.outcomes import effect_failure_record

    body = b'{"error":"request rejected"}'
    source = SimpleNamespace(request=AsyncMock(return_value=HttpResponse(status, {}, body)))
    original = http_execution()
    with pytest.raises(HttpResponseError) as caught:
        await http_provider(tmp_path, source).apply(original, credential=None)
    observation = caught.value.observations[0]
    assert observation.content["http_status"] == status
    assert (
        tmp_path / str(original.run_id) / observation.content["file"]["path"]
    ).read_bytes() == body
    record = effect_failure_record(
        capability=original.capability_version,
        provider="http",
        attempt_no=1,
        code=caught.value.code,
        retryable=False,
        previous=None,
    )
    assert record == {"code": "http_response_error", "retryable": False}
    assert source.request.await_count == 1


async def test_http_revocation_prevents_response_file_publication(tmp_path):
    """送信後の撤権でも再送せず、正文 file は公開しない。"""
    source = SimpleNamespace(request=AsyncMock(return_value=HttpResponse(200, {}, b"private")))
    authorize = AsyncMock(side_effect=[None, None, PermissionError("revoked")])
    original = http_execution()
    with pytest.raises(PermissionError):
        await http_provider(tmp_path, source, authorize).apply(original, credential=None)
    assert not list((tmp_path / str(original.run_id)).rglob("response.bin"))
    assert source.request.await_count == 1


class Connection:
    """実行・commit・rollback を別々に故障注入できる接続替身。"""

    def __init__(self, *, commit_error=None, rollback_error=None):
        self.commands = []
        self.commit_error, self.rollback_error = commit_error, rollback_error
        self.committed = self.rolled_back = False
        self.driver = SimpleNamespace(execute=AsyncMock(return_value="UPDATE 1"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, value, traceback):
        return None

    def begin(self):
        """外側の接続解放と transaction 終了を区別する。"""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def transaction():
            try:
                yield
            except BaseException:
                if self.rollback_error:
                    raise self.rollback_error from None
                self.rolled_back = True
                raise
            else:
                if self.commit_error:
                    raise self.commit_error
                self.committed = True

        return transaction()

    async def exec_driver_sql(self, sql):
        self.commands.append(sql)

    async def get_raw_connection(self):
        return SimpleNamespace(driver_connection=self.driver)


def sql_execution():
    """事前 SELECT や原回執表に依存しない最小 UPDATE を固定する。"""
    return replace(
        execution(),
        provider="postgres",
        capability_version="database.execute/v1",
        operation="UPDATE",
        integration_config={"access_mode": "native_sql"},
        integration_scope={"statements": ["SELECT", "UPDATE"]},
        target={"locator": "database", "display": "database"},
        changes=(
            {
                "path": "/statement",
                "action": "SET",
                "value": {
                    "statement": {
                        "sql": "UPDATE public.items SET done=$1 WHERE id=$2",
                        "parameters": [True, 1],
                    },
                },
            },
        ),
        precondition={},
        verification={"method": "SQL_RESPONSE", "paths": ["/statement"]},
    )


@pytest.fixture
def sql_connection(monkeypatch):
    """資格や本番接続を使わず原 Provider の transaction 境界を検査する。"""
    from skillmind.effects import postgres_native as module

    connection = Connection()
    engine = SimpleNamespace(connect=lambda: connection, dispose=AsyncMock())
    factory = AsyncMock()
    # 同期 factory 呼出しも含めて再 claim の無送信を確認する。
    from unittest.mock import Mock

    factory = Mock(return_value=engine)
    monkeypatch.setattr(module, "create_database_engine", factory)
    return connection, engine, factory


@pytest.mark.parametrize("command_tag,count", [("INSERT 0 1", 1), ("UPDATE 3", 3), ("DELETE 0", 0)])
async def test_sql_executes_once_without_external_receipts_or_business_reads(
    sql_connection, command_tag, count
):
    """原 DML と commit 事実を返し、0 行や業務状態を成功判定にすり替えない。"""
    connection, engine, _ = sql_connection
    operation = command_tag.split()[0]
    statements = {
        "INSERT": "INSERT INTO public.items(id) VALUES ($1)",
        "UPDATE": "UPDATE public.items SET done=true WHERE id=$1",
        "DELETE": "DELETE FROM public.items WHERE id=$1",
    }
    original = replace(
        sql_execution(),
        operation=operation,
        integration_scope={"statements": [operation]},
        changes=(
            {
                "path": "/statement",
                "action": "SET",
                "value": {
                    "statement": {"sql": statements[operation], "parameters": [1]},
                },
            },
        ),
    )
    connection.driver.execute.return_value = command_tag
    authorize = AsyncMock()
    result = await NativeDatabaseWriteProvider(authorize).apply(
        original, credential="fixture-secret"
    )
    assert connection.driver.execute.await_count == 1
    assert connection.driver.execute.await_args.args[1:] == (1,)
    assert connection.committed and not connection.rolled_back
    assert connection.commands == [
        "SET LOCAL statement_timeout = '15s'",
        "SET LOCAL lock_timeout = '2s'",
    ]
    assert result.after.content == {"command_tag": command_tag, "affected_rows": count}
    assert result.before.content == original.changes[0]["value"]
    assert result.verification["method"] == "SQL_RESPONSE"
    assert result.verification["committed"] is True
    assert result.verification["business_verdict"] == "NOT_EVALUATED"
    assert not result.replayed
    assert authorize.await_count == 4
    assert all(args.args == (original, "fixture-secret") for args in authorize.await_args_list)
    engine.dispose.assert_awaited_once()


async def test_sql_unknown_reclaim_never_connects_or_resends(sql_connection):
    """原 receipt を作らず、二回目 claim は変更を再送せず不明を保つ。"""
    connection, _, factory = sql_connection
    with pytest.raises(EffectProviderTransportError, match="sql_result_unconfirmed"):
        await NativeDatabaseWriteProvider(AsyncMock()).apply(
            replace(sql_execution(), attempt_no=2), credential="fixture-secret"
        )
    factory.assert_not_called()
    connection.driver.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "state,code",
    [
        ("42703", "sql_undefined_column"),
        ("42P01", "sql_undefined_table"),
        ("42501", "sql_permission_denied"),
        ("23505", "sql_constraint_violation"),
        ("22007", "sql_invalid_parameter"),
        ("42601", "sql_syntax_error"),
        ("57014", "sql_transaction_rolled_back"),
    ],
)
async def test_sql_error_is_correctable_only_after_rollback(sql_connection, state, code):
    """SQLSTATE の固定診断を返し、原例外・SQL・parameter を漏らさない。"""
    from asyncpg import PostgresError

    from skillmind.effects.outcomes import effect_failure_record
    from skillmind.effects.postgres_native import sql_failure_message

    connection, _, _ = sql_connection
    error = PostgresError("private SQL, values and credential")
    error.sqlstate = state
    connection.driver.execute.side_effect = error
    with pytest.raises(SqlExecutionError) as caught:
        await NativeDatabaseWriteProvider(AsyncMock()).apply(
            sql_execution(), credential="fixture-secret"
        )
    assert caught.value.code == code and connection.rolled_back
    assert not connection.committed
    record = effect_failure_record(
        capability="database.execute/v1",
        provider="postgres",
        attempt_no=1,
        code=code,
        retryable=False,
        previous=None,
    )
    assert record == {"code": code, "retryable": False}
    assert "private" not in str(caught.value)
    assert "rolled back" in sql_failure_message(code)
    # 過去 attempt の不明結果を、今回の rollback で確定済みに変えない。
    assert (
        effect_failure_record(
            capability="database.execute/v1",
            provider="postgres",
            attempt_no=2,
            code=code,
            retryable=False,
            previous={"code": "effect_result_unknown"},
        )["code"]
        == "effect_result_unknown"
    )


@pytest.mark.parametrize("phase", ["commit", "rollback"])
async def test_sql_unconfirmed_transaction_is_never_correctable_or_replayed(sql_connection, phase):
    """commit 応答喪失と rollback 失敗は、既知 SQL エラーでも不明を維持する。"""
    from asyncpg import PostgresError

    from skillmind.effects.outcomes import effect_failure_record

    connection, _, _ = sql_connection
    if phase == "commit":
        connection.commit_error = OSError("private connection detail")
    else:
        connection.driver.execute.side_effect = PostgresError("private query")
        connection.rollback_error = OSError("rollback unavailable")
    with pytest.raises(EffectProviderTransportError) as caught:
        await NativeDatabaseWriteProvider(AsyncMock()).apply(
            sql_execution(), credential="fixture-secret"
        )
    assert caught.value.code == "sql_result_unconfirmed"
    assert (
        effect_failure_record(
            capability="database.execute/v1",
            provider="postgres",
            attempt_no=1,
            code=caught.value.code,
            retryable=False,
            previous=None,
        )["code"]
        == "effect_result_unknown"
    )
    assert connection.driver.execute.await_count == 1


async def test_connection_failure_proves_sql_not_sent(sql_connection):
    """接続前の失敗は SQL 未送信として返し、結果不明へ昇格させない。"""
    connection, engine, _ = sql_connection
    engine.connect = lambda: (_ for _ in ()).throw(OSError("private DSN"))
    with pytest.raises(SqlExecutionError, match="sql_request_not_sent"):
        await NativeDatabaseWriteProvider(AsyncMock()).apply(
            sql_execution(), credential="fixture-secret"
        )
    connection.driver.execute.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("phase", ["initial_authorization", "engine", "before_send"])
@pytest.mark.parametrize("failure", [TypeError, RuntimeError])
async def test_local_failure_before_sql_is_not_an_unknown_write(
    sql_connection, phase, failure,
):
    """ローカル故障を未送信と記録し、例外本文や資格・SQL を公開しない。"""
    from skillmind.effects.outcomes import effect_failure_record

    connection, engine, factory = sql_connection
    error = failure("private SQL, values and credentials")
    authorize = AsyncMock()
    if phase == "initial_authorization":
        authorize.side_effect = error
    elif phase == "engine":
        factory.side_effect = error
    else:
        authorize.side_effect = [None, error, None]
    with pytest.raises(SqlExecutionError) as caught:
        await NativeDatabaseWriteProvider(authorize).apply(
            sql_execution(), credential="fixture-secret",
        )
    assert caught.value.code == "sql_request_not_sent"
    assert "private" not in str(caught.value)
    assert effect_failure_record(
        capability="database.execute/v1", provider="postgres", attempt_no=1,
        code=caught.value.code, retryable=False, previous=None,
    ) == {"code": "sql_request_not_sent", "retryable": False}
    connection.driver.execute.assert_not_awaited()
    assert not connection.committed
    if phase == "before_send":
        assert connection.rolled_back
        engine.dispose.assert_awaited_once()
    else:
        engine.dispose.assert_not_awaited()


async def test_local_failure_after_commit_is_still_unknown(sql_connection):
    """commit 後の認可故障を未送信へ変換せず、原操作の再送を防ぐ。"""
    from skillmind.effects.outcomes import effect_failure_record

    connection, engine, _ = sql_connection
    authorize = AsyncMock(side_effect=[None, None, None, TypeError("private detail")])
    with pytest.raises(TypeError):
        await NativeDatabaseWriteProvider(authorize).apply(
            sql_execution(), credential="fixture-secret",
        )
    assert connection.committed and connection.driver.execute.await_count == 1
    assert effect_failure_record(
        capability="database.execute/v1", provider="postgres", attempt_no=1,
        code="effect_provider_failed", retryable=False, previous=None,
    )["code"] == "effect_result_unknown"
    engine.dispose.assert_awaited_once()


async def test_sql_prepare_error_preserves_its_structured_diagnostic(sql_connection):
    """DML 送信前でも PostgreSQL が確認した列エラーは、接続故障と混同しない。"""
    from asyncpg import PostgresError

    connection, _, _ = sql_connection
    error = PostgresError("private SQL")
    error.sqlstate = "42703"
    connection.get_raw_connection = AsyncMock(side_effect=error)
    with pytest.raises(SqlExecutionError, match="sql_undefined_column"):
        await NativeDatabaseWriteProvider(AsyncMock()).apply(
            sql_execution(), credential="fixture-secret",
        )
    connection.driver.execute.assert_not_awaited()
    assert connection.rolled_back and not connection.committed


@pytest.mark.parametrize("failure", [
    PermissionError, asyncio.CancelledError, EffectLeaseValidationError,
])
async def test_initial_sql_authority_failure_keeps_its_original_semantics(sql_connection, failure):
    """初回の取消・撤権・失権を未送信の通常エラーへ畳まず、そのまま終了する。"""
    connection, engine, factory = sql_connection
    with pytest.raises(failure):
        await NativeDatabaseWriteProvider(AsyncMock(side_effect=failure())).apply(
            sql_execution(), credential="fixture-secret",
        )
    factory.assert_not_called()
    connection.driver.execute.assert_not_awaited()
    engine.dispose.assert_not_awaited()


async def test_local_failure_on_reclaim_preserves_the_unknown_original_result(sql_connection):
    """過去 attempt の結果不明は、今回のローカル故障でも未送信へ上書きしない。"""
    connection, _, factory = sql_connection
    with pytest.raises(EffectProviderTransportError, match="sql_result_unconfirmed"):
        await NativeDatabaseWriteProvider(AsyncMock(side_effect=TypeError("private detail"))).apply(
            replace(sql_execution(), attempt_no=2), credential="fixture-secret",
        )
    factory.assert_not_called()
    connection.driver.execute.assert_not_awaited()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("failure", [PermissionError, asyncio.CancelledError])
async def test_sql_revocation_and_cancellation_do_not_become_query_errors(
    sql_connection, when, failure
):
    """送信前認可と commit 前監督を保ち、取消を SQL エラーへ変換しない。"""
    connection, engine, _ = sql_connection
    authorize = AsyncMock(
        side_effect=[None, failure()] if when == "before" else [None, None, failure()]
    )
    with pytest.raises(failure):
        await NativeDatabaseWriteProvider(authorize).apply(
            sql_execution(), credential="fixture-secret"
        )
    assert connection.driver.execute.await_count == (0 if when == "before" else 1)
    assert connection.rolled_back and not connection.committed
    engine.dispose.assert_awaited_once()
