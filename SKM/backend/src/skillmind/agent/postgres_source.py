"""指定 PostgreSQL テーブルをパラメータ化した読取専用 SQL で取得する。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import URL, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from skillmind.agent.postgres_schema import identifier as identifier
from skillmind.agent.postgres_schema import read_table_schema
from skillmind.core.hashing import canonical_json

MAX_DATABASE_BYTES = 1_048_576
MAX_DATABASE_ROWS = 100


class DatabaseReadError(RuntimeError):
    """接続先・SQL・資格情報を含まない読取失敗。"""


@dataclass(frozen=True, slots=True)
class DatabaseQuery:
    """モデルの構造化要求から確定した SQL 以外の読取条件。"""

    table: str
    columns: tuple[str, ...]
    filters: Mapping[str, Any]
    order_by: tuple[str, ...]
    limit: int
    offset: int
    include_schema: bool = False


@dataclass(frozen=True, slots=True)
class DatabaseRows:
    """一回の live snapshot に含まれた行と、未表示データの有無。"""

    rows: tuple[dict[str, Any], ...]
    truncated: bool
    table_schema: dict[str, Any] | None = None


class DatabaseSource(Protocol):
    """外部 DB の I/O を Run の権限・Evidence 処理から分離する。"""

    async def read(
        self,
        config: Mapping[str, Any],
        password: str,
        query: DatabaseQuery,
    ) -> DatabaseRows:
        """許可済み接続に対し、一回の有界読取を行う。"""
        ...


def build_database_query(arguments: Mapping[str, Any]) -> DatabaseQuery:
    """Gateway 外から呼ばれても SQL 断片や無限取得を受け付けない。"""
    if set(arguments) - {
        "table", "columns", "filters", "order_by", "limit", "offset", "purpose", "include_schema"
    }:
        raise ValueError("Unknown database read field")
    table = arguments.get("table")
    if not isinstance(table, str) or len(table.split(".")) != 2:
        raise ValueError("Expected schema.table")
    for part in table.split("."):
        identifier(part)
    columns = arguments.get("columns", [])
    order_by = arguments.get("order_by", [])
    for values in (columns, order_by):
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("Invalid database columns")
        for value in values:
            identifier(value)
        if len(set(values)) != len(values):
            raise ValueError("Duplicate database columns")
    filters = arguments.get("filters", {})
    if not isinstance(filters, dict) or len(filters) > 20:
        raise ValueError("Invalid database filters")
    for key, value in filters.items():
        identifier(key)
        if value is not None and type(value) not in {str, int, float, bool}:
            raise ValueError("Invalid database filter value")
        if isinstance(value, str) and len(value) > 2000:
            raise ValueError("Database filter is too large")
    limit, offset = arguments.get("limit", MAX_DATABASE_ROWS), arguments.get("offset", 0)
    if type(limit) is not int or not 1 <= limit <= MAX_DATABASE_ROWS:
        raise ValueError("Invalid database row limit")
    if type(offset) is not int or not 0 <= offset <= 10_000:
        raise ValueError("Invalid database offset")
    include_schema = arguments.get("include_schema", False)
    if type(include_schema) is not bool:
        raise ValueError("Invalid database schema option")
    return DatabaseQuery(
        table, tuple(columns), dict(filters), tuple(order_by), limit, offset, include_schema
    )


def database_statement(
    query: DatabaseQuery, *, byte_limit: int = MAX_DATABASE_BYTES,
) -> tuple[str, dict[str, Any]]:
    """識別子と値を分離し、巨大行は wire に出す前に NULL へ置換する。"""
    table = ".".join(identifier(part) for part in query.table.split("."))
    columns = ", ".join(identifier(column) for column in query.columns) or "*"
    params: dict[str, Any] = {
        "row_limit": query.limit + 1,
        "row_offset": query.offset,
        "byte_limit": byte_limit,
    }
    predicates = []
    for index, (column, value) in enumerate(query.filters.items()):
        key = f"filter_{index}"
        predicates.append(f"{identifier(column)} IS NOT DISTINCT FROM :{key}")
        params[key] = value
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    order = (
        " ORDER BY " + ", ".join(identifier(c) for c in query.order_by) if query.order_by else ""
    )
    sql = (
        "SELECT octet_length(payload) AS size, "
        "CASE WHEN octet_length(payload) <= :byte_limit THEN payload END AS payload "
        "FROM (SELECT row_to_json(selected)::text AS payload FROM ("
        f"SELECT {columns} FROM {table}{where}{order} LIMIT :row_limit OFFSET :row_offset"
        ") AS selected) AS bounded"
    )
    return sql, params


async def read_database_rows(
    connection: AsyncConnection, query: DatabaseQuery, *, byte_limit: int = MAX_DATABASE_BYTES,
) -> DatabaseRows:
    """一件ずつ消費し、100 行/合計 byte のいずれかで停止する。"""
    sql, params = database_statement(query, byte_limit=byte_limit)
    rows: list[dict[str, Any]] = []
    size = 0
    async with connection.stream(text(sql), params, execution_options={"yield_per": 1}) as result:
        async for row in result.mappings():
            if len(rows) >= query.limit or row["payload"] is None:
                return DatabaseRows(tuple(rows), True)
            payload = row["payload"]
            size += len(payload.encode("utf-8"))
            if size > byte_limit:
                return DatabaseRows(tuple(rows), True)
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise DatabaseReadError("Database returned an invalid row")
            rows.append(value)
    return DatabaseRows(tuple(rows), False)


class PostgresDatabaseSource:
    """接続の環境 fallback を使わず、設定済み host/user/TLS だけで読み取る。"""

    async def read(
        self,
        config: Mapping[str, Any],
        password: str,
        query: DatabaseQuery,
    ) -> DatabaseRows:
        """短い専用接続と read-only transaction を閉じ、失敗理由を外部に反射しない。"""
        engine = create_database_engine(config, password, read_only=True)
        try:
            async with asyncio.timeout(15), engine.connect() as connection, connection.begin():
                await connection.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                if query.include_schema:
                    table = ".".join(identifier(part) for part in query.table.split("."))
                    # 列観測と行読取の間に DDL が入り、異なる table 版を混ぜない。
                    await connection.exec_driver_sql(f"LOCK TABLE {table} IN ACCESS SHARE MODE")
                    table_schema = await read_table_schema(connection, quoted_table=table)
                    schema_bytes = len(canonical_json(table_schema).encode("utf-8"))
                    remaining = MAX_DATABASE_BYTES - schema_bytes
                    rows = await read_database_rows(connection, query, byte_limit=remaining)
                    return DatabaseRows(rows.rows, rows.truncated, table_schema)
                return await read_database_rows(connection, query)
        except (SQLAlchemyError, OSError, TimeoutError, ValueError) as error:
            raise DatabaseReadError("Database read could not be completed") from error
        finally:
            await engine.dispose()


def create_database_engine(
    config: Mapping[str, Any], password: str, *, read_only: bool,
) -> AsyncEngine:
    """接続情報と timeout を共通化し、読取/承認済み書込の既定 transaction を分離する。"""

    return create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=config["username"],
            password=password,
            host=config["host"],
            port=config["port"],
            database=config["database"],
        ),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "ssl": config["sslmode"],
            "timeout": 5,
            "command_timeout": 7,
            "server_settings": {
                "default_transaction_read_only": "on" if read_only else "off",
                "statement_timeout": "5000",
                "lock_timeout": "2000",
            },
        },
    )
