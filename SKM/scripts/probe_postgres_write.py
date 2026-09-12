"""PGlite の隔離 DB で SQL・回执・rollback を実行する。実 server/並行接続の証拠ではない。"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from skillmind.effects.database_write import build_database_write
from skillmind.effects.postgres_write import (
    DatabaseWriteConflictError,
    apply_database_write,
    lookup_database_write,
)
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg


class Result:
    """PGlite の実返却を SQLAlchemy result の読取面へ写像する。"""

    def __init__(self, value):
        """行と実 affectedRows を保持する。"""
        self.value = value
        self.rowcount = value.get("affectedRows", 0)

    def mappings(self):
        """列名で読む結果を返す。"""
        return self

    def one_or_none(self):
        """一行条件を実返却件数へ適用する。"""
        rows = self.value["rows"]
        assert len(rows) <= 1
        return rows[0] if rows else None

    def one(self):
        """必須の一行を返す。"""
        value = self.one_or_none()
        assert value is not None
        return value

    def __iter__(self):
        """metadata の全行を列挙する。"""
        return iter(self.value["rows"])


class Connection:
    """生成した parameterized SQL を実 PGlite engine に渡す stdio adapter。"""

    def __init__(self, process):
        """専用の Node process を保持する。"""
        self.process = process

    async def query(self, sql, params=None, batch=False):
        """SQL と値を分離した JSON を送り、実 DB のエラーを失敗にする。"""
        self.process.stdin.write(
            (
                json.dumps({"sql": sql, "params": params or [], "batch": batch}, default=str) + "\n"
            ).encode()
        )
        await self.process.stdin.drain()
        response = await asyncio.wait_for(self.process.stdout.readline(), timeout=20)
        if not response:
            raise RuntimeError("PGlite process ended before returning a SQL result")
        result = json.loads(response)
        if not result["ok"]:
            raise RuntimeError(result)
        return Result(result["result"]) if not batch else result["result"]

    async def execute(self, statement, params=None):
        """本番と同じ asyncpg dialect の positional bind を使う。"""
        compiled = statement.compile(dialect=PGDialect_asyncpg())
        return await self.query(
            str(compiled), [(params or {})[name] for name in compiled.positiontup or []]
        )

    async def exec_driver_sql(self, sql):
        """transaction 設定など値を持たない文を実行する。"""
        return await self.query(sql)

    @asynccontextmanager
    async def begin(self):
        """実 BEGIN/COMMIT/ROLLBACK を engine に送る。"""
        await self.query("BEGIN")
        try:
            yield
        except BaseException:
            await self.query("ROLLBACK")
            raise
        else:
            await self.query("COMMIT")


async def authorize():
    """合成 probe の許可点。実 actor/binding 検証の代替ではない。"""


async def probe(bridge: Path, pglite_root: Path, *, rv_reviewer: bool = False):
    """メモリ DB で合成 schema と原 Effect の観測・変更・回执を実行する。"""
    process = await asyncio.create_subprocess_exec(
        "node",
        str(bridge),
        str(pglite_root),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        db = Connection(process)
        await db.query(
            (Path(__file__).resolve().parent / "sql/postgres-effect-receipts.sql").read_text(),
            batch=True,
        )
        await db.query(
            "CREATE SCHEMA example; CREATE TABLE example.reviews("
            "id uuid PRIMARY KEY, status text NOT NULL, result jsonb, finished_at timestamptz, "
            "derived text GENERATED ALWAYS AS (status || '-derived') STORED)",
            batch=True,
        )
        await db.query(
            "CREATE ROLE effect_writer; "
            "GRANT USAGE ON SCHEMA example, skillmind_effects TO effect_writer; "
            "GRANT SELECT, INSERT, UPDATE ON example.reviews TO effect_writer; "
            "GRANT SELECT, INSERT ON skillmind_effects.execution_receipts TO effect_writer; "
            "SET ROLE effect_writer",
            batch=True,
        )
        base = {
            "effect_id": uuid4(),
            "project_id": uuid4(),
            "run_id": uuid4(),
            "integration_id": uuid4(),
            "table": "example.reviews",
            "operation": "INSERT",
            "key": {"id": str(uuid4())},
            "values": {"status": "RUNNING"},
            "expected": None,
            "scope": {
                "tables": ["example.reviews"],
                "operations": ["INSERT", "UPDATE"],
                "write_columns": [f"example.reviews.{column}"
                                  for column in ["id", "status", "result", "finished_at"]],
            },
        }
        original = build_database_write(**base)
        inserted = await apply_database_write(db, original, authorize=authorize)
        assert inserted.after["derived"] == "RUNNING-derived"
        assert (await apply_database_write(db, original, authorize=authorize)).replayed
        update = build_database_write(
            **{
                **base,
                "effect_id": uuid4(),
                "operation": "UPDATE",
                "values": {
                    "status": "COMPLETED",
                    "result": {"verdict": "PASS", "text": "x'); DROP TABLE example.reviews; --"},
                    "finished_at": "2026-09-11T10:00:00Z",
                },
                "expected": inserted.after,
            }
        )
        changed = await apply_database_write(db, update, authorize=authorize)
        assert changed.after["derived"] == "COMPLETED-derived"
        assert changed.after["result"]["verdict"] == "PASS"
        assert (await lookup_database_write(db, original)).after == inserted.after
        try:
            await apply_database_write(
                db, build_database_write(**{**base, "effect_id": uuid4()}), authorize=authorize
            )
        except DatabaseWriteConflictError:
            pass
        else:
            raise AssertionError("same-value insert accepted")
        stale = build_database_write(
            **{**base, "effect_id": uuid4(), "operation": "UPDATE", "expected": inserted.after}
        )
        try:
            await apply_database_write(db, stale, authorize=authorize)
        except DatabaseWriteConflictError:
            pass
        else:
            raise AssertionError("stale update accepted")
        calls = 0

        async def revoke():
            """SQL 回读後の失権で transaction を rollback させる。"""
            nonlocal calls
            calls += 1
            if calls == 3:
                raise PermissionError("revoked")

        cancelled = build_database_write(
            **{
                **base,
                "effect_id": uuid4(),
                "operation": "UPDATE",
                "expected": changed.after,
                "values": {"status": "CANCELLED"},
            }
        )
        try:
            await apply_database_write(db, cancelled, authorize=revoke)
        except PermissionError:
            pass
        else:
            raise AssertionError("revocation accepted")
        assert await lookup_database_write(db, cancelled) is None
        current = (await db.query("SELECT status FROM example.reviews")).one()
        assert current["status"] == "COMPLETED"
        try:
            await db.query("DELETE FROM skillmind_effects.execution_receipts")
        except RuntimeError as error:
            assert "42501" in str(error)
        else:
            raise AssertionError("receipt deletion was allowed")
        print(
            "PGlite SQL probe passed: insert, typed update, generated columns, "
            "original receipt replay, stale rejection, SQL value binding, "
            "revocation rollback, restricted receipt role"
        )
        if rv_reviewer:
            from probe_rv_reviewer_database import probe_rv_reviewer

            await db.query("RESET ROLE")
            await probe_rv_reviewer(db)
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()


_BRIDGE = """
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
const { PGlite } = createRequire(process.argv[2] + '/package.json')('@electric-sql/pglite');
const db = new PGlite();
await db.waitReady;
for await (const line of createInterface({ input: process.stdin })) {
 const request = JSON.parse(line);
 try {
  const result = request.batch ? await db.exec(request.sql)
    : await db.query(request.sql, request.params || []);
  process.stdout.write(JSON.stringify({ok:true,result}) + '\\n');
 } catch (error) {
  process.stdout.write(JSON.stringify({ok:false,message:error.message,code:error.code}) + '\\n');
 }
}
await db.close();
"""


def main() -> None:
    """明示された PGlite package を使い、独立一時 bridge を終了時に回収する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pglite-root", type=Path, required=True)
    parser.add_argument(
        "--rv-reviewer", action="store_true", help="Also verify the RV business schema"
    )
    args = parser.parse_args()
    package_root = args.pglite_root.resolve(strict=True)
    if not (package_root / "node_modules/@electric-sql/pglite/package.json").is_file():
        parser.error("Install @electric-sql/pglite into the supplied package root first")
    with tempfile.TemporaryDirectory(prefix="skillmind-pg-probe-") as temp:
        bridge = Path(temp) / "bridge.mjs"
        bridge.write_text(_BRIDGE)
        asyncio.run(probe(bridge, package_root, rv_reviewer=args.rv_reviewer))


if __name__ == "__main__":
    main()
