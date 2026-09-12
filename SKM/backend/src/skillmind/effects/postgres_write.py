"""一行の変更と原 Effect 回执を同じ PostgreSQL transaction に保存する。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from skillmind.agent.postgres_source import MAX_DATABASE_BYTES, create_database_engine, identifier
from skillmind.core.hashing import canonical_json
from skillmind.effects.database_write import DatabaseWriteCommand, build_database_write

_RECEIPTS = '"skillmind_effects"."execution_receipts"'


class DatabaseWriteConflictError(RuntimeError):
    """主キー・原状態または原 Effect identity が要求と一致しない。"""


class DatabaseWriteUncertainError(RuntimeError):
    """接続/commit の成否を断定できず、原 Effect の回执確認が必要である。"""


@dataclass(frozen=True, slots=True)
class DatabaseWriteReceipt:
    """同一 transaction で保存した原前後行を保持し、今日の同値行と区別する。"""

    before: dict[str, Any] | None
    after: dict[str, Any]
    replayed: bool


class PostgresDatabaseWriteSource:
    """承認済み実行と原回执照会に専用の有界接続を用意する。自動再送はしない。"""

    async def apply(
        self,
        config: Mapping[str, Any],
        password: str,
        command: DatabaseWriteCommand,
        *,
        authorize: Callable[[], Awaitable[None]],
    ) -> DatabaseWriteReceipt:
        """元接続先でのみ実行し、接続終了まで caller の取消を伝播させる。"""

        async def action(connection: AsyncConnection) -> DatabaseWriteReceipt:
            """一回の批准済み transaction を共有実装へ委譲する。"""

            return await apply_database_write(connection, command, authorize=authorize)

        result = await self._connected(
            config, password, action, read_only=False, authorize=authorize
        )
        assert result is not None
        return result

    async def lookup(
        self,
        config: Mapping[str, Any],
        password: str,
        command: DatabaseWriteCommand,
        *,
        authorize: Callable[[], Awaitable[None]],
    ) -> DatabaseWriteReceipt | None:
        """原要求を只読接続で照会し、返却前にも現在の参照権を確認する。"""

        async def action(connection: AsyncConnection) -> DatabaseWriteReceipt | None:
            """原 identity の確認だけを行い、変更処理へ退避しない。"""

            result = await lookup_database_write(connection, command)
            await authorize()
            return result

        return await self._connected(config, password, action, read_only=True, authorize=authorize)

    async def _connected(
        self,
        config: Mapping[str, Any],
        password: str,
        action: Callable[[AsyncConnection], Awaitable[DatabaseWriteReceipt | None]],
        *,
        read_only: bool,
        authorize: Callable[[], Awaitable[None]],
    ) -> DatabaseWriteReceipt | None:
        """接続生成・終了を一箇所で制御し、結果未知を原 identity のまま返す。"""

        frozen_config = dict(config)
        await authorize()
        engine = create_database_engine(frozen_config, password, read_only=read_only)
        try:
            async with asyncio.timeout(20), engine.connect() as connection:
                return await action(connection)
        except PermissionError:
            raise
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise DatabaseWriteUncertainError(
                "Database effect connection could not be confirmed"
            ) from error
        finally:
            await engine.dispose()


async def apply_database_write(
    connection: AsyncConnection,
    command: DatabaseWriteCommand,
    *,
    authorize: Callable[[], Awaitable[None]],
) -> DatabaseWriteReceipt:
    """専用接続で一回だけ transaction を開き、現行批准・実行権を復験して変更する。"""

    _verify_command(command)
    try:
        async with asyncio.timeout(15), connection.begin():
            await connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ WRITE"
            )
            await connection.exec_driver_sql("SET LOCAL statement_timeout = '5000'")
            await connection.exec_driver_sql("SET LOCAL lock_timeout = '2000'")
            await authorize()
            # 同じ identity の並行呼出は先行 transaction の commit/rollback を待つ。
            # hash 衝突は余分な直列化に留まり、同名/同値による replay 判定には使わない。
            lock_key = int.from_bytes(command.effect_id.bytes[:8], "big", signed=True)
            await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
            previous = await _receipt(connection, command)
            if previous is not None:
                await authorize()
                return previous
            await _validate_table(connection, command)
            before = await _row(connection, command)
            if canonical_json(before) != command.expected_json:
                raise DatabaseWriteConflictError(
                    "Database row no longer matches the observed state"
                )
            desired = (
                {**command.key, **command.values}
                if command.operation == "INSERT"
                else (command.values)
            )
            converted = await _typed_values(connection, command, desired)
            # 待機・観測後に再検証する。Scope/lease の復験は caller の共有認可入口が行う。
            await authorize()
            await _mutate(connection, command, desired)
            after = await _row(connection, command)
            if after is None or any(after.get(key) != value for key, value in converted.items()):
                raise DatabaseWriteConflictError(
                    "Database write did not retain the approved values"
                )
            await connection.execute(
                text(
                    f"INSERT INTO {_RECEIPTS} (effect_id, request_checksum, before_row, after_row) "
                    "VALUES (:effect_id, :checksum, CAST(:before AS jsonb), CAST(:after AS jsonb))"
                ),
                {
                    "effect_id": command.effect_id,
                    "checksum": command.checksum,
                    "before": canonical_json(before),
                    "after": canonical_json(after),
                },
            )
            await authorize()
            return DatabaseWriteReceipt(before, after, False)
    except PermissionError:
        # 認可 callback の明示拒否は transport 不明と分類しない。
        raise
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        # commit の失敗も含む。例外正文には SQL・パラメータ・接続情報が入り得る。
        raise DatabaseWriteUncertainError("Confirm the original database effect receipt") from error


async def lookup_database_write(
    connection: AsyncConnection,
    command: DatabaseWriteCommand,
) -> DatabaseWriteReceipt | None:
    """結果未知の原 identity だけを READ ONLY で照会し、不在を再送許可に変えない。"""

    _verify_command(command)
    try:
        async with asyncio.timeout(10), connection.begin():
            await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            await connection.exec_driver_sql("SET LOCAL statement_timeout = '5000'")
            return await _receipt(connection, command)
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        raise DatabaseWriteUncertainError(
            "Database effect receipt could not be confirmed"
        ) from error


def _verify_command(command: DatabaseWriteCommand) -> None:
    """手製 dataclass や差替えた JSON を原 fingerprint のまま実行しない。"""

    rebuilt = build_database_write(
        effect_id=command.effect_id,
        project_id=command.project_id,
        run_id=command.run_id,
        integration_id=command.integration_id,
        table=command.table,
        operation=command.operation,
        key=command.key,
        values=command.values,
        expected=command.expected,
        scope={
            "tables": [command.table],
            "operations": [command.operation],
            "write_columns": [f"{command.table}.{key}"
                              for key in sorted({*command.values, *command.key})],
        },
    )
    if rebuilt != command:
        raise ValueError("Database effect command checksum is invalid")


async def _receipt(
    connection: AsyncConnection,
    command: DatabaseWriteCommand,
) -> DatabaseWriteReceipt | None:
    """回执の checksum と有界前後行を照合し、別要求・不完全回执を成功にしない。"""

    result = await connection.execute(
        text(
            "SELECT request_checksum, "
            "CASE WHEN octet_length(before_row::text) <= :limit "
            "THEN before_row::text END AS before, "
            "CASE WHEN octet_length(after_row::text) <= :limit THEN after_row::text END AS after "
            f"FROM {_RECEIPTS} WHERE effect_id = :effect_id"
        ),
        {"effect_id": command.effect_id, "limit": MAX_DATABASE_BYTES},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    if row["request_checksum"] != command.checksum:
        raise DatabaseWriteConflictError("Database effect identity belongs to another request")
    before = _decode_row(row["before"], allow_null=True)
    after = _decode_row(row["after"])
    assert after is not None
    receipt = DatabaseWriteReceipt(before, after, True)
    validate_database_write_receipt(command, receipt)
    return receipt


def validate_database_write_receipt(
    command: DatabaseWriteCommand, receipt: DatabaseWriteReceipt,
) -> None:
    """読取と核対保存で原前後行の同じ検証を使い、不完全な回执を拒否する。"""

    if (canonical_json(receipt.before) != command.expected_json
        or not isinstance(receipt.after, dict)
        or not {*command.key, *command.values}.issubset(receipt.after)):
        raise DatabaseWriteConflictError(
            "Database effect receipt does not contain the original facts"
        )


async def _validate_table(connection: AsyncConnection, command: DatabaseWriteCommand) -> None:
    """実 PRIMARY KEY 全列と書込可能列を確認し、view/generated/identity 更新を拒否する。"""

    result = await connection.execute(
        text(
            "SELECT a.attname AS name, a.attgenerated AS generated, a.attidentity AS identity, "
            "EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conrelid = c.oid "
            "AND k.contype = 'p' AND a.attnum = ANY(k.conkey)) AS primary_key "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
            "WHERE c.oid = to_regclass(:table) AND c.relkind IN ('r', 'p') "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": _table(command)},
    )
    columns = {row["name"]: row for row in result.mappings()}
    primary_key = {name for name, row in columns.items() if row["primary_key"]}
    if not primary_key or set(command.key) != primary_key:
        raise DatabaseWriteConflictError("Database write must identify the complete primary key")
    for name in {*command.values, *command.key}:
        column = columns.get(name)
        if column is None or column["generated"] or column["identity"]:
            raise DatabaseWriteConflictError("Database column cannot be written explicitly")


def _table(command: DatabaseWriteCommand) -> str:
    """検査した schema/table の各部を独立に引用する。"""

    return ".".join(identifier(part) for part in command.table.split("."))


def _predicate(command: DatabaseWriteCommand) -> str:
    """実 table 型へ変換した主キー値だけを比較し、モデル SQL を生成しない。"""

    return " AND ".join(
        f"t.{identifier(name)} IS NOT DISTINCT FROM "
        f"(SELECT k.{identifier(name)} FROM jsonb_populate_record("
        f"NULL::{_table(command)}, CAST(:key AS jsonb)) k)"
        for name in sorted(command.key)
    )


async def _row(
    connection: AsyncConnection,
    command: DatabaseWriteCommand,
) -> dict[str, Any] | None:
    """更新の原行を lock し、挿入は SELECT/INSERT 権限だけで有界回読する。"""

    # 不在行は row lock で保護できない。INSERT の競争は精確主キーの UNIQUE 制約で
    # 拒否し、挿入後は同じ transaction の書込 lock が保持する。FOR UPDATE を一律に
    # 要求すると、成果/監査 table の insert-only role に不要な UPDATE 権限が必要になる。
    lock = " FOR UPDATE" if command.operation == "UPDATE" else ""
    result = await connection.execute(
        text(
            "SELECT CASE WHEN octet_length(to_jsonb(t)::text) <= :limit "
            "THEN to_jsonb(t)::text END AS payload "
            f"FROM {_table(command)} t WHERE {_predicate(command)}{lock}"
        ),
        {"key": command.key_json, "limit": MAX_DATABASE_BYTES},
    )
    row = result.mappings().one_or_none()
    return None if row is None else _decode_row(row["payload"])


async def _typed_values(
    connection: AsyncConnection,
    command: DatabaseWriteCommand,
    values: dict[str, Any],
) -> dict[str, Any]:
    """PostgreSQL 自身の型変換で承認値を正規化し、日時/UUID/JSON の回読と比較する。"""

    result = await connection.execute(
        text(
            "SELECT CASE WHEN octet_length(to_jsonb(v)::text) <= :limit "
            "THEN to_jsonb(v)::text END AS payload FROM jsonb_populate_record("
            f"NULL::{_table(command)}, CAST(:values AS jsonb)) v"
        ),
        {"values": canonical_json(values), "limit": MAX_DATABASE_BYTES},
    )
    converted = _decode_row(result.mappings().one()["payload"])
    assert converted is not None
    return {key: converted[key] for key in values}


async def _mutate(
    connection: AsyncConnection,
    command: DatabaseWriteCommand,
    values: dict[str, Any],
) -> None:
    """指定列だけを INSERT/UPDATE し、default と generated 列は DB に任せる。"""

    names = sorted(values)
    typed_record = f"jsonb_populate_record(NULL::{_table(command)}, CAST(:values AS jsonb)) v"
    if command.operation == "INSERT":
        columns = ", ".join(identifier(name) for name in names)
        projected = ", ".join(f"v.{identifier(name)}" for name in names)
        statement = (
            f"INSERT INTO {_table(command)} ({columns}) SELECT {projected} FROM {typed_record}"
        )
    else:
        assignments = ", ".join(f"{identifier(name)} = v.{identifier(name)}" for name in names)
        statement = (
            f"UPDATE {_table(command)} t SET {assignments} FROM {typed_record} "
            f"WHERE {_predicate(command)}"
        )
    result = await connection.execute(
        text(statement),
        {
            "values": canonical_json(values),
            "key": command.key_json,
        },
    )
    if result.rowcount != 1:
        raise DatabaseWriteConflictError("Database write did not affect exactly one row")


def _decode_row(payload: object, *, allow_null: bool = False) -> dict[str, Any] | None:
    """SQL NULL/巨大/非 object をエラーとし、JSON null だけを INSERT の原不在と扱う。"""

    if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_DATABASE_BYTES:
        raise DatabaseWriteConflictError("Database row or receipt is invalid or too large")
    value = json.loads(payload)
    if value is None and allow_null:
        return None
    if not isinstance(value, dict):
        raise DatabaseWriteConflictError("Database row or receipt must be an object")
    return value
