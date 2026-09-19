"""目录・履歴 repository の SQL/rollback/FK 用 SQLite seam。PG lock は検証しない。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, MetaData, create_engine, event
from sqlalchemy.orm import Session

from skillmind.db import models as m


class SqlPort:
    """実 Session を repository の非同期 port に接続する。"""

    def __init__(self, session):
        self.session = session

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def scalars(self, statement):
        return self.session.scalars(statement)

    async def execute(self, statement):
        return self.session.execute(statement)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def delete(self, row):
        self.session.delete(row)

    async def flush(self):
        self.session.flush()

    def add(self, row):
        self.session.add(row)


class SqlDatabase:
    """FK と唯一性は維持し、PG 専用 CHECK だけを別検証へ分離する。"""

    def __init__(self):
        self.engine = create_engine("sqlite://")
        event.listen(self.engine, "connect", lambda db, _: db.execute("PRAGMA foreign_keys=ON"))
        self.metadata = MetaData()
        for table in m.Base.metadata.tables.values():
            clone = table.to_metadata(self.metadata)
            for check in list(clone.constraints):
                if isinstance(check, CheckConstraint):
                    clone.constraints.remove(check)
            for index in clone.indexes:
                condition = index.dialect_options["postgresql"].get("where")
                if condition is not None:
                    index.dialect_options["sqlite"]["where"] = condition
        self.metadata.create_all(self.engine)
        self.rows = {}

    def seed(self, name, /, **overrides):
        """必須 FK を実 parent へ結び、合成履歴の依存 graph を作る。"""
        if name in self.rows:
            return self.rows[name]
        table = self.metadata.tables[name]
        row = {"id": uuid4(), **overrides}
        self.rows[name] = row
        for column in table.columns:
            if column.name in row:
                continue
            if column.nullable:
                row[column.name] = None
                continue
            foreign = next(iter(column.foreign_keys), None)
            if foreign:
                parent = self.seed(foreign.column.table.name)
                row[column.name] = parent[foreign.column.name]
            else:
                kind = column.type.python_type
                row[column.name] = (
                    {
                        dict: {},
                        list: [],
                        str: "fixture",
                        int: 1,
                        float: 0.5,
                        bool: True,
                        bytes: b"x",
                        datetime: datetime.now(UTC),
                    }.get(kind)
                    if kind.__name__ not in {"UUID", "Decimal"}
                    else uuid4()
                    if kind.__name__ == "UUID"
                    else 1
                )
        with self.engine.begin() as connection:
            connection.execute(table.insert().values(**row))
        return row

    @contextmanager
    def transaction(self):
        """失敗時は実 transaction を rollback する。"""
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            yield SqlPort(session)
