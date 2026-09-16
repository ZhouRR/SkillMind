"""PGlite の隔離メモリ DB で本番 Source の分類・純構造・rollback を確認する。"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.exc import DBAPIError

from probe_postgres_write import _BRIDGE, Connection
from skillmind.agent.postgres_source import (
    DatabaseReadError,
    PostgresDatabaseSource,
    build_database_query,
)


class ReadConnection(Connection):
    """SQLSTATE を構造化例外へ写し、本番 stream 消費をメモリ PostgreSQL へ接続する。"""

    async def query(self, sql, params=None, batch=False):
        """例外正文は分類へ渡さず、実 engine の code 属性だけを保持する。"""
        try:
            return await super().query(sql, params, batch)
        except RuntimeError as error:
            result = error.args[0]
            if not isinstance(result, dict) or "code" not in result:
                raise
            original = Exception("Synthetic driver failure")
            original.sqlstate = result["code"]
            raise DBAPIError(None, None, original) from None

    @asynccontextmanager
    async def stream(self, statement, params=None, **kwargs):
        """有限の合成行を Source と同じ mapping interface に渡す。"""
        result = await self.execute(statement, params)
        yield Stream(result)


class Stream:
    """Source が逐次消費する mapping result。"""

    def __init__(self, result):
        """この query の返却だけを保持する。"""
        self.result = result

    def mappings(self):
        """列名アクセスを提供する。"""
        return self

    async def __aiter__(self):
        """後続 query を発行せず一回の結果を列挙する。"""
        for row in self.result:
            yield row


class Engine:
    """実接続を作らず、同じ isolated DB の独立 transaction を提供する。"""

    def __init__(self, db):
        """専用 process の connection を使用する。"""
        self.db = db

    @asynccontextmanager
    async def connect(self):
        """Source が開始・終了する transaction の外側を表す。"""
        yield self.db

    async def dispose(self):
        """共有するのは隔離 process のみ。外部 pool は存在しない。"""


async def probe(bridge: Path, package_root: Path):
    """原失敗を確認後、新 transaction で構造確認と訂正を行う固定例。"""
    process = await asyncio.create_subprocess_exec(
        "node",
        str(bridge),
        str(package_root),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        db = ReadConnection(process)
        await db.query(
            "CREATE SCHEMA sample; CREATE TABLE sample.records("
            "run_id integer NOT NULL, document_id integer NOT NULL, kind text NOT NULL, "
            "PRIMARY KEY(document_id,run_id,kind)); "
            "CREATE TABLE sample.no_key(value text); CREATE ROLE reader; "
            "GRANT USAGE ON SCHEMA sample TO reader; "
            "GRANT SELECT ON sample.records,sample.no_key TO reader;",
            batch=True,
        )
        source = PostgresDatabaseSource()
        with patch(
            "skillmind.agent.postgres_source.create_database_engine",
            return_value=Engine(db),
        ):
            # 空表でも元定義の主鍵順序を保持し、id を捏造しない。
            schema = (await source.describe({}, "", "sample.records")).table_schema
            assert schema["primary_key"] == ["document_id", "run_id", "kind"]
            assert (await source.describe({}, "", "sample.no_key")).table_schema[
                "primary_key"
            ] == []
            try:
                await source.read(
                    {},
                    "",
                    build_database_query(
                        {"table": "sample.records", "filters": {"artifact_id": 1}}
                    ),
                )
            except DatabaseReadError as error:
                assert error.diagnostic.code == "invalid_request"
                assert error.diagnostic.reason_code == "undefined_column"
                assert error.diagnostic.stage == "row_read"
            else:
                raise AssertionError("Expected a real undefined-column error")
            # 失敗 transaction は終了済み。同じ接続の次 transaction でも回復できる。
            observed = await source.describe({}, "", "sample.records")
            corrected = await source.read(
                {},
                "",
                build_database_query(
                    {
                        "table": "sample.records",
                        "filters": {
                            key: value
                            for key, value in zip(
                                observed.table_schema["primary_key"],
                                [1, 2, "SOURCE"],
                                strict=True,
                            )
                        },
                    }
                ),
            )
            assert corrected.rows == () and not corrected.truncated
            await db.query("ALTER TABLE sample.records ADD COLUMN added text")
            assert "added" in [
                c["name"]
                for c in (await source.describe({}, "", "sample.records")).table_schema[
                    "columns"
                ]
            ]
            try:
                await source.describe({}, "", "sample.missing")
            except DatabaseReadError as error:
                assert error.diagnostic.code == "not_found"
            else:
                raise AssertionError("Missing table must fail")
            await db.query(
                "REVOKE SELECT ON sample.records FROM reader; SET ROLE reader",
                batch=True,
            )
            try:
                await source.describe({}, "", "sample.records")
            except DatabaseReadError as error:
                assert error.diagnostic.code == "scope_denied"
            else:
                raise AssertionError("Revoked access must fail")
        print(
            "PGlite: empty/composite/no-key, 42703, rollback, correction, DDL refresh, 42P01, 42501 passed"
        )
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()


def main():
    """指定済み package のみ使用し、外部モデル・DB・production Run は呼ばない。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pglite-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.pglite_root.resolve(strict=True)
    if not (root / "node_modules/@electric-sql/pglite/package.json").is_file():
        parser.error("PGlite is not installed in the supplied package root")
    with tempfile.TemporaryDirectory(prefix="skillmind-read-probe-") as temp:
        bridge = Path(temp) / "bridge.mjs"
        bridge.write_text(_BRIDGE)
        asyncio.run(probe(bridge, root))


if __name__ == "__main__":
    main()
