"""登録 DB account の権限で実行する原生 SQL。構文・transaction の境界だけを固定する。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from asyncpg import Connection
from pglast import parse_sql
from pglast.stream import RawStream
from sqlalchemy.ext.asyncio import AsyncConnection

from skillmind.agent.postgres_source import create_database_engine
from skillmind.core.hashing import canonical_json, sha256_hex

MAX_QUERY_BYTES = 16 * 1024 * 1024

# PostgreSQL 組込型の OID のみ。名称が同じ user-defined type は変換しない。
_TEMPORAL_PARAMETER_TYPES = {
    1082: "date",
    1083: "time",
    1114: "timestamp",
    1184: "timestamptz",
    1266: "timetz",
}


def native_statement(
    request: Mapping[str, Any], scope: Mapping[str, Any], *, write: bool
) -> tuple[str, tuple[Any, ...], str]:
    """PG parser で単一 DML を確認し、DDL/SET/COPY/transaction 制御を受け付けない。"""
    if not isinstance(request, Mapping):
        raise ValueError("SQL request must be an object")
    if set(request) - {"sql", "parameters", "limit", "purpose", "response_mode"}:
        raise ValueError("Unknown SQL request field")
    sql, parameters = request.get("sql"), request.get("parameters", [])
    if (
        not isinstance(sql, str)
        or not 1 <= len(sql.encode("utf-8")) <= 65536
        or not isinstance(parameters, list)
        or len(parameters) > 1000
        or any(
            value is not None and type(value) not in {str, int, float, bool}
            for value in parameters
        )
    ):
        raise ValueError("SQL or parameters are invalid")
    try:
        statements = parse_sql(sql)
    except Exception:
        # Parser の診断には SQL 本文が含まれ得るため、固定文言のみ返す。
        raise ValueError("SQL could not be parsed") from None
    if len(statements) != 1:
        raise ValueError("One SQL statement is required")
    tree = statements[0].stmt
    commands = {
        "SelectStmt": "SELECT",
        "InsertStmt": "INSERT",
        "UpdateStmt": "UPDATE",
        "DeleteStmt": "DELETE",
    }
    operation = commands.get(type(tree).__name__)
    if operation not in ({"INSERT", "UPDATE", "DELETE"} if write else {"SELECT"}):
        raise ValueError("SQL requires the matching query or approved execute capability")

    def check(node: Any) -> None:
        """CTE 内の変更も検査し、SELECT INTO と row lock を読取へ混ぜない。"""
        if isinstance(node, dict):
            kind = node.get("@")
            if (
                kind == "OnConflictClause"
                and node.get("action", {}).get("name") == "ONCONFLICT_UPDATE"
                and "UPDATE" not in scope.get("statements", [])
            ):
                raise ValueError("SQL upsert requires UPDATE permission")
            if (
                write
                and kind == "RangeVar"
                and (
                    node.get("schemaname")
                    in {"skillmind_effects", "pg_catalog", "information_schema"}
                    or node.get("relname") == "execution_receipts"
                )
            ):
                raise ValueError("Native SQL cannot mutate platform receipts or system relations")
            if kind in commands and commands[kind] not in scope.get("statements", []):
                raise ValueError("SQL operation is outside the connection scope")
            if kind in commands and not write and kind != "SelectStmt":
                raise ValueError("Query cannot contain a modifying CTE")
            if node.get("intoClause") is not None or (not write and node.get("lockingClause")):
                raise ValueError("Query cannot create tables or lock rows")
            for value in node.values():
                check(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                check(value)

    check(tree(skip_none=True))
    return str(RawStream()(tree)), tuple(parameters), str(operation)


def query_identity(request: Mapping[str, Any], scope: Mapping[str, Any]) -> str:
    """表示目的や file 配送方式を除き、同じ SELECT・parameter・上限を一意に照合する。"""
    sql, parameters, _ = native_statement(request, scope, write=False)
    limit = request.get("limit", 1000)
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("Query limit must be between 1 and 10000")
    return "sha256:" + sha256_hex(
        canonical_json(
            {
                "sql": sql,
                "parameters": list(parameters),
                "limit": limit,
            }
        )
    )


async def bind_native_parameters(
    driver: Connection, sql: str, parameters: tuple[Any, ...]
) -> tuple[Any, ...]:
    """JSON の日時文字列を、実 SQL の parameter 型と同じ transaction で変換する。"""
    if not any(isinstance(value, str) for value in parameters):
        return parameters
    types = (await driver.prepare(sql)).get_parameters()
    if len(types) != len(parameters):
        raise ValueError("SQL parameter count does not match the statement")
    slots = [
        (index, _TEMPORAL_PARAMETER_TYPES[parameter_type.oid])
        for index, parameter_type in enumerate(types)
        if parameter_type.oid in _TEMPORAL_PARAMETER_TYPES
        and isinstance(parameters[index], str)
    ]
    if not slots:
        return parameters
    # 値を SQL に埋め込まない。PG の timezone/DateStyle/特殊日時の意味を保ち、
    # Python のローカル時区や文字列の見た目で日時を推測しない。
    expressions = [
        f"${position}::text::pg_catalog.{type_name}"
        for position, (_, type_name) in enumerate(slots, start=1)
    ]
    converted = await driver.fetchrow(
        "SELECT " + ", ".join(expressions), *(parameters[index] for index, _ in slots)
    )
    if converted is None or len(converted) != len(slots):
        raise ValueError("SQL temporal parameter conversion is incomplete")
    values = list(parameters)
    for (index, _), value in zip(slots, converted, strict=True):
        values[index] = value
    return tuple(values)


async def query_rows(
    connection: AsyncConnection, request: Mapping[str, Any], scope: Mapping[str, Any]
) -> dict[str, Any]:
    """SQL の全結果を model に返さず、有界 JSON file 用の byte 同等表現を作る。"""
    sql, parameters, _ = native_statement(request, scope, write=False)
    limit = request.get("limit", 1000)
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("Query limit must be between 1 and 10000")
    rows: list[Any] = []
    size = 0
    # SQL 本文は PG AST から一文として出力済み。LIMIT は検証済み integer のみ。
    raw_connection = await connection.get_raw_connection()
    driver = raw_connection.driver_connection
    if driver is None:
        raise ValueError("PostgreSQL driver is unavailable")
    parameters = await bind_native_parameters(driver, sql, parameters)
    cursor = driver.cursor(
        "SELECT CASE WHEN octet_length(payload) <= "
        + str(MAX_QUERY_BYTES)
        + " THEN payload ELSE NULL END FROM (SELECT row_to_json(s)::text AS payload "
        + f"FROM ({sql}) s LIMIT {limit + 1}) bounded",
        *parameters,
        prefetch=1,
    )
    async for item in cursor:
        raw = item[0]
        if raw is None:
            return {"rows": rows, "truncated": True}
        size += len(raw.encode("utf-8"))
        if len(rows) >= limit or size > MAX_QUERY_BYTES:
            return {"rows": rows, "truncated": True}
        rows.append(json.loads(raw))
    return {"rows": rows, "truncated": False}


class NativePostgresSource:
    """各要求に専用接続を作り、transaction/timeout/現在権限を client が管理する。"""

    async def read(
        self,
        config: Mapping[str, Any],
        password: str,
        request: Mapping[str, Any],
        scope: Mapping[str, Any],
        *,
        authorize: Callable[[], Awaitable[None]],
    ) -> dict[str, Any]:
        """DB の READ ONLY と登録 account ACL の両方を適用する。"""
        native_statement(request, scope, write=False)
        engine = create_database_engine(config, password, read_only=True)
        try:
            async with asyncio.timeout(30), engine.connect() as connection, connection.begin():
                await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                await connection.exec_driver_sql("SET LOCAL statement_timeout = '15000'")
                await connection.exec_driver_sql("SET LOCAL lock_timeout = '2000'")
                await authorize()
                result = await query_rows(connection, request, scope)
                await authorize()
                return result
        finally:
            await engine.dispose()
