"""履歴の DTO を変えず、SQL が大きな非表示列を読まないことを検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import AgentTaskBriefSnapshot, Run
from skillmind.runs.repository import RunRepository


class _Rows:
    """SQL は実 DB に送らず、読取 repository が消費する行だけを返す。"""

    def __init__(self, values: list[Any]) -> None:
        """取得済み行の合成結果を保持する。"""
        self.values = values

    def all(self) -> list[Any]:
        """順序を保った行集合。"""
        return self.values

    def one_or_none(self) -> Any:
        """0/1 行の lookup 結果。"""
        assert len(self.values) <= 1
        return self.values[0] if self.values else None


class _Session:
    """本番 Select を捕捉する。正しい権限/DB transaction の証明には使わない。"""

    def __init__(self, run: Run | None = None) -> None:
        """任意の既知 Run と SQL 集合を保持する。"""
        self.run = run
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        """履歴 page は空結果にして、問い合わせ shape を検証する。"""
        self.statements.append(statement)
        return _Rows([])

    async def scalars(self, statement: Any) -> _Rows:
        """詳細の Run だけ返し、付随する明細は空集合とする。"""
        self.statements.append(statement)
        entity = statement.column_descriptions[0].get("entity")
        return _Rows([self.run] if entity is Run and self.run is not None else [])


async def test_history_omits_large_snapshot_and_result_columns() -> None:
    """Project/filter/pagination を保ったまま不要な JSON 列を取得しない。"""
    session = _Session()
    result = await RunRepository(cast(AsyncSession, session)).list_history(
        project_id=uuid4(), limit=20, offset=0,
    )
    assert result.items == () and result.has_more is False
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    for field in ("task_snapshot_json", "permission_snapshot_json", "limits_snapshot_json", "data_json"):
        assert field not in sql
    for field in ("summary", "confidence", "needs_review", "input_json", "selected_sources_json"):
        assert field in sql
    assert "LEFT OUTER JOIN" in sql and "LIMIT" in sql and "OFFSET" in sql
    assert "project_id" in sql


async def test_detail_loads_brief_checksum_without_source_body() -> None:
    """既存 scalar lookup seam と返却 shape を維持し、Brief 本文だけを外す。"""
    now = datetime.now(UTC)
    run = Run(
        id=uuid4(), project_id=uuid4(), task_id=uuid4(), status="FAILED", row_version=1,
        created_at=now, updated_at=now, started_at=now, finished_at=now,
        input_json={}, selected_sources_json={}, task_snapshot_json={},
        permission_snapshot_json={}, limits_snapshot_json={}, error_json=None,
    )
    session = _Session(run)
    detail = await RunRepository(cast(AsyncSession, session)).get_detail(
        project_id=run.project_id, run_id=run.id,
    )
    assert detail.run.run_id == run.id and detail.result is None
    statements = [str(stmt.compile(dialect=postgresql.dialect())) for stmt in session.statements]
    brief_sql = next(sql for sql in statements if AgentTaskBriefSnapshot.__tablename__ in sql)
    assert "checksum" in brief_sql and "run_segment_id" in brief_sql
    assert "brief_json" not in brief_sql
