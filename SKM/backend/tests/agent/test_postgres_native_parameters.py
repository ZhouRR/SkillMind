"""JSON 日時 parameter の型解決、同一 transaction での変換と読取/書込の共用を検証する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from asyncpg import InvalidDatetimeFormatError

from skillmind.agent.postgres_native import bind_native_parameters, query_identity, query_rows
from skillmind.effects.postgres_native import NativeDatabaseWriteProvider
from skillmind.effects.redmine import EffectProviderTransportError
from tests.effects.test_native_resource_effects import Connection, sql_execution


def driver_with_types(oids, converted):
    """PG の型解決と日時 cast の I/O だけを置換する。"""
    statement = SimpleNamespace(
        get_parameters=lambda: tuple(SimpleNamespace(oid=oid) for oid in oids)
    )
    return SimpleNamespace(
        prepare=AsyncMock(return_value=statement),
        fetchrow=AsyncMock(return_value=converted),
        execute=AsyncMock(),
    )


@pytest.mark.parametrize(
    "oid,type_name,value,converted",
    [
        (1082, "date", "2030-01-02", date(2030, 1, 2)),
        (1083, "time", "12:34:56", time(12, 34, 56)),
        (1114, "timestamp", "2030-01-02 12:34:56", datetime(2030, 1, 2, 12, 34, 56)),
        (
            1184,
            "timestamptz",
            "2030-01-02 12:34:56+09",
            datetime(2030, 1, 2, 3, 34, 56, tzinfo=UTC),
        ),
        (1266, "timetz", "12:34:56+09", time(12, 34, 56, tzinfo=timezone(timedelta(hours=9)))),
    ],
)
async def test_temporal_strings_use_postgres_resolved_type(oid, type_name, value, converted):
    """cast 構文の有無で推測せず、PG の型 OID を使い、値は bind のまま渡す。"""
    sql = "UPDATE public.items SET observed=$1 WHERE id=$2"
    driver = driver_with_types([oid, 23], [converted])
    original = (value, 42)
    assert await bind_native_parameters(driver, sql, original) == (converted, 42)
    driver.prepare.assert_awaited_once_with(sql)
    driver.fetchrow.assert_awaited_once_with(f"SELECT $1::text::pg_catalog.{type_name}", value)
    assert original == (value, 42)


async def test_mixed_parameters_keep_order_text_json_null_and_original_identity():
    """日時に似た text や JSON の値を変換せず、元の観測 hash 入力も変更しない。"""
    values = ["2030-01-02", '{"date":"2030-01-02"}', "2030-01-02", None, True, 42]
    request = {
        "sql": "SELECT $1::date,$2::jsonb,$3::text,$4::timestamp,$5::bool,$6::int",
        "parameters": values,
    }
    identity = query_identity(request, {"statements": ["SELECT"]})
    driver = driver_with_types([1082, 3802, 25, 1114, 16, 23], [date(2030, 1, 2)])
    actual = await bind_native_parameters(driver, request["sql"], tuple(values))
    assert actual == (date(2030, 1, 2), *values[1:])
    assert query_identity(request, {"statements": ["SELECT"]}) == identity
    assert values[0] == "2030-01-02"


@pytest.mark.parametrize("oid", [25, 2950, 999999])
async def test_non_temporal_and_user_defined_types_are_not_guessed(oid):
    """text/UUID/custom type の名前や値の見た目から日時型へ変更しない。"""
    driver = driver_with_types([oid], [])
    original = ("2030-01-02",)
    assert await bind_native_parameters(driver, "SELECT $1", original) is original
    driver.fetchrow.assert_not_awaited()


async def test_numeric_parameters_do_not_add_an_extra_database_call():
    """文字列がない従来 SELECT/DML には型照会を増やさない。"""
    driver = driver_with_types([], [])
    original = (42, False, None, 1.25)
    assert await bind_native_parameters(driver, "SELECT $1,$2,$3,$4", original) is original
    driver.prepare.assert_not_awaited()
    driver.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("failure", [InvalidDatetimeFormatError, asyncio.CancelledError])
async def test_invalid_dates_and_cancellation_are_not_hidden(failure):
    """PG の拒否や取消を空値・推測値へ置き換えず、既存の例外境界へ戻す。"""
    driver = driver_with_types([1082], [])
    driver.fetchrow.side_effect = failure()
    with pytest.raises(failure):
        await bind_native_parameters(driver, "SELECT $1::date", ("invalid-date",))


async def test_query_rows_binds_temporal_parameters_without_changing_bounded_cursor():
    """実 read 経路も変換を通り、row_to_json・上限・小分け取得を保つ。"""
    driver = driver_with_types([1082], [date(2030, 1, 2)])

    async def rows(*args, **kwargs):
        """上限内の JSON 行を一つ返す有界 cursor。"""
        yield ('{"day":"2030-01-02"}',)

    driver.cursor = Mock(side_effect=rows)
    connection = SimpleNamespace(
        get_raw_connection=AsyncMock(return_value=SimpleNamespace(driver_connection=driver))
    )
    result = await query_rows(
        connection,
        {
            "sql": "SELECT $1::date AS day",
            "parameters": ["2030-01-02"],
            "limit": 1,
        },
        {"statements": ["SELECT"]},
    )
    assert result == {"rows": [{"day": "2030-01-02"}], "truncated": False}
    assert driver.cursor.call_args.args[1:] == (date(2030, 1, 2),)
    assert "LIMIT 2" in driver.cursor.call_args.args[0]
    assert driver.cursor.call_args.kwargs == {"prefetch": 1}


@pytest.mark.parametrize("invalid", [False, True])
async def test_native_write_converts_before_dml_and_keeps_receipt_transaction(monkeypatch, invalid):
    """日付付き SQL を実 Provider へ渡し、変換失敗時には DML と回执を送信しない。"""
    from skillmind.effects import postgres_native as module

    connection = Connection()
    expected_date = date(2030, 1, 2)
    expected_time = datetime(2030, 1, 2, 3, 34, 56, tzinfo=UTC)
    connection.driver = driver_with_types([1082, 1184, 23], [expected_date, expected_time])
    if invalid:
        connection.driver.fetchrow.side_effect = InvalidDatetimeFormatError()
    monkeypatch.setattr(
        module,
        "create_database_engine",
        lambda *a, **kw: SimpleNamespace(
            connect=lambda: connection,
            dispose=AsyncMock(),
        ),
    )
    monkeypatch.setattr(
        module,
        "query_rows",
        AsyncMock(
            side_effect=[
                {"rows": [{"done": False}], "truncated": False},
                {"rows": [{"done": True}], "truncated": False},
            ]
        ),
    )
    original = sql_execution()
    value = {
        **original.changes[0]["value"],
        "statement": {
            "sql": "UPDATE public.items SET day=$1,observed=$2 WHERE id=$3",
            "parameters": ["2030-01-02", "2030-01-02 12:34:56+09", 42],
        },
    }
    execution = replace(original, changes=({**original.changes[0], "value": value},))
    authorize = AsyncMock()
    provider = NativeDatabaseWriteProvider(authorize)
    if invalid:
        with pytest.raises(EffectProviderTransportError):
            await provider.apply(execution, credential="fixture-password")
        connection.driver.execute.assert_not_awaited()
        assert connection.saved is None and all(connection.exits)
    else:
        result = await provider.apply(execution, credential="fixture-password")
        assert result.verification["method"] == "READ_BACK"
        assert connection.saved is not None and connection.exits == [None, None]
        assert connection.driver.execute.await_args.args[1:] == (expected_date, expected_time, 42)
        assert authorize.await_count == 3
