"""構造 cache の候補選択を軽量化し、観測のない Run へ追加 I/O を課さない。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from skillmind.agent.database_observations import DatabaseObservations
from skillmind.agent.database_provider import DatabaseReadProvider
from skillmind.agent.runtime_policy import RUNTIME_POLICY
from sqlalchemy.dialects import postgresql


class CapturingSession:
    """実 SQLAlchemy の statement を記録し、外部 DB には接続しない。"""

    def __init__(self) -> None:
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [])

    async def scalars(self, statement):
        self.statements.append(statement)
        return []


def context():
    """同一 Run と binding で query shape を比較する最小 context。"""
    return SimpleNamespace(
        run_id=uuid4(),
        tool=SimpleNamespace(
            integration_id=uuid4(),
            binding_id=uuid4(),
        ),
    )


async def test_lookup_requires_a_json_object_not_merely_non_sql_null() -> None:
    """JSON null や配列を最新構造として選び、正しい過去観測を隠さない。"""
    session = CapturingSession()
    store = DatabaseObservations(lambda: session)
    assert await store.lookup(context(), SimpleNamespace(checksum="fixed"), "public.items") is None
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    assert "jsonb_typeof" in str(compiled)
    assert "object" in compiled.params.values()
    assert "evidence.id DESC" in str(compiled)


async def test_table_index_groups_before_limiting() -> None:
    """一つの表への多数の読取で別表の構造が索引から追い出されない。"""
    session = CapturingSession()
    store = DatabaseObservations(lambda: session)
    assert await store.tables(context(), datetime.now(UTC)) == []
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "GROUP BY" in sql
    assert "max(" in sql
    assert "jsonb_typeof" in sql
    assert 20 in compiled.params.values()


async def test_non_database_run_does_not_query_schema_observations() -> None:
    """DB Tool のない現在の Run には cache 読取という追加前提を作らない。"""
    provider = DatabaseReadProvider(
        lambda: None,
        source=SimpleNamespace(),
        secret_resolver=SimpleNamespace(),
    )
    provider._observations = SimpleNamespace(
        segment_index=AsyncMock(side_effect=AssertionError("unnecessary database access")),
    )
    claimed = SimpleNamespace(
        task_snapshot_json={"runtime_policy": RUNTIME_POLICY},
        run_segment_id=uuid4(),
        run_id=uuid4(),
    )
    assert await provider.project_facts(claimed, [], SimpleNamespace()) == []
    provider._observations.segment_index.assert_not_awaited()
