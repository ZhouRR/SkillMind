"""許可済み単一 table の列/主キーだけを有界に記述する。値や式は取得しない。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from skillmind.core.hashing import canonical_json

MAX_SCHEMA_COLUMNS = 100
MAX_SCHEMA_BYTES = 65_536
_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_$]{0,62}\Z")
_COLUMN_FIELDS = {"name", "data_type", "not_null", "has_default", "generated", "identity"}


def identifier(value: str) -> str:
    """読取/構造観測/書込で同じ識別子文法を検証し、値とは独立に引用する。"""

    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid database identifier")
    return f'"{value}"'


def validate_table_schema(value: object) -> dict[str, Any]:
    """source と Provider で同じ完全性/型/容量制約を適用し、新しい JSON object を返す。"""

    if not isinstance(value, Mapping) or set(value) != {"columns", "primary_key"}:
        raise ValueError("Database schema is invalid")
    columns, primary = value["columns"], value["primary_key"]
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_SCHEMA_COLUMNS:
        raise ValueError("Database schema columns exceed the supported limit")
    names = []
    for column in columns:
        if not isinstance(column, Mapping) or set(column) != _COLUMN_FIELDS:
            raise ValueError("Database schema column is invalid")
        name = column["name"]
        identifier(name)
        if name in names:
            raise ValueError("Database schema column name is invalid")
        names.append(name)
        if (
            not isinstance(column["data_type"], str)
            or not 1 <= len(column["data_type"]) <= 1000
            or any(
                type(column[key]) is not bool for key in ("not_null", "has_default", "generated")
            )
            or column["identity"] not in ("NONE", "ALWAYS", "BY_DEFAULT")
        ):
            raise ValueError("Database schema column attributes are invalid")
    if (
        not isinstance(primary, list)
        or len(primary) > MAX_SCHEMA_COLUMNS
        or any(not isinstance(key, str) or key not in names for key in primary)
        or len(set(primary)) != len(primary)
    ):
        raise ValueError("Database schema primary key is invalid")
    result = {"columns": [dict(column) for column in columns], "primary_key": list(primary)}
    if len(canonical_json(result).encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise ValueError("Database schema exceeds the byte limit")
    return result


async def read_table_schema(connection: AsyncConnection, *, quoted_table: str) -> dict[str, Any]:
    """同一 transaction で caller が読取 lock した table を調べ、欠落/部分結果を拒否する。"""

    result = await connection.execute(
        text(
            "SELECT a.attname AS name, "
            "pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type, "
            "a.attnotnull AS not_null, a.atthasdef AS has_default, "
            "a.attgenerated <> '' AS generated, "
            "CASE a.attidentity WHEN 'a' THEN 'ALWAYS' "
            "WHEN 'd' THEN 'BY_DEFAULT' ELSE 'NONE' END AS identity, "
            "array_position(k.conkey, a.attnum) AS key_position "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
            "LEFT JOIN pg_catalog.pg_constraint k ON k.conrelid = c.oid AND k.contype = 'p' "
            "WHERE c.oid = to_regclass(:table) AND c.relkind IN ('r', 'p') "
            "AND a.attnum > 0 AND NOT a.attisdropped "
            "AND has_table_privilege(c.oid, 'SELECT') "
            "ORDER BY a.attnum LIMIT :column_limit"
        ),
        {"table": quoted_table, "column_limit": MAX_SCHEMA_COLUMNS + 1},
    )
    records = list(result.mappings())
    primary = sorted(
        (row["key_position"], row["name"]) for row in records if row["key_position"] is not None
    )
    return validate_table_schema(
        {
            "columns": [{key: row[key] for key in _COLUMN_FIELDS} for row in records],
            "primary_key": [name for _position, name in primary],
        }
    )
