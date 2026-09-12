"""SQLite の実 JOIN で DB 観測の来歴選択を検証する。PG 制約/lock の証拠ではない。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, create_engine
from sqlalchemy.orm import Session

from skillmind.db.models import Evidence, ToolCall
from skillmind.effects.database_write import database_row_revision
from skillmind.effects.domain import ChangeProposalValidationError
from skillmind.runs.repository import RunRepository


class QuerySession:
    """本番 SELECT をローカル SQLite へ渡す await adapter。業務 write は実行しない。"""

    def __init__(self, session):
        """同じメモリ DB の ORM session を保持する。"""
        self.session = session

    async def scalars(self, statement):
        """SQL 条件を stub 判定せず、実 JOIN/filter で返す。"""
        return self.session.scalars(statement)


@pytest.fixture
def observation():
    """PG 専用 CHECK/FK を含めず、検索に使う二表の列だけをメモリ DB へ複製する。"""
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    for model in (Evidence, ToolCall):
        Table(
            model.__tablename__,
            metadata,
            *[
                Column(column.name, column.type, primary_key=column.primary_key)
                for column in model.__table__.columns
            ],
        )
    metadata.create_all(engine)
    with Session(engine) as session:
        run_id, integration_id, tool_id = uuid4(), uuid4(), uuid4()
        binding = SimpleNamespace(
            run_id=run_id, integration_id=integration_id, checksum="binding-hash"
        )
        draft = SimpleNamespace(
            capability_version="database.write/v1", evidence_refs=("ev_database001",)
        )
        payload = {
            "table": "example.records",
            "operation": "INSERT",
            "key": {"id": "row-1"},
            "values": {"status": "RUNNING"},
            "expected": None,
        }
        tool = ToolCall(
            id=tool_id,
            run_id=run_id,
            run_attempt_id=uuid4(),
            agent_session_id=uuid4(),
            sdk_tool_use_id="tool-read",
            request_fingerprint="a" * 64,
            tool_name="postgres_read",
            capability_version="database.read/v1",
            provider="postgres",
            integration_id=integration_id,
            arguments_summary={},
            status="SUCCEEDED",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        evidence = Evidence(
            id=uuid4(),
            evidence_ref="ev_database001",
            run_id=run_id,
            tool_call_id=tool_id,
            evidence_type="database",
            source_uri="postgres://integration/example",
            source_locator={
                "integration_id": str(integration_id),
                "table": "example.records",
                "offset": 0,
                "columns": [],
                "filters": {"id": "row-1"},
                "row_count": 0,
                "truncated": False,
                "row_hashes": [],
            },
            content_hash="sha256:" + "a" * 64,
            metadata_json={"binding_checksum": "binding-hash"},
            created_at=datetime.now(UTC),
        )
        session.add_all([tool, evidence])
        yield SimpleNamespace(
            repository=RunRepository(QuerySession(session)),
            binding=binding,
            draft=draft,
            payload=payload,
            tool=tool,
            evidence=evidence,
        )
    engine.dispose()


async def verify(h):
    """公開済み観測を照合する本番 repository method を呼ぶ。"""
    await h.repository._validate_database_observation(h.draft, binding=h.binding, payload=h.payload)


async def test_exact_absence_and_full_original_row_are_accepted(observation):
    """成功 read の原 filter・全行 hash が一致する INSERT/UPDATE だけを通す。"""
    h = observation
    await verify(h)
    before = {"id": "row-1", "status": "OLD"}
    h.payload.update(operation="UPDATE", expected=before)
    h.evidence.source_locator = {
        **h.evidence.source_locator,
        "row_count": 1,
        "row_hashes": [database_row_revision(before)],
    }
    await verify(h)


@pytest.mark.parametrize(
    "change",
    [
        "evidence-run",
        "tool-run",
        "integration",
        "failed",
        "pending",
        "write-tool",
        "provider",
        "missing-tool",
        "binding",
        "table",
        "filters",
        "projection",
        "truncated",
        "offset",
        "count",
        "revision",
        "evidence-ref",
    ],
)
async def test_untrusted_or_inexact_observation_cannot_authorize_proposal(observation, change):
    """自己申告・別実行・失敗 Tool・部分行・異条件の Evidence を実 SELECT で排除する。"""
    h = observation
    if change == "evidence-run":
        h.evidence.run_id = uuid4()
    elif change == "tool-run":
        h.tool.run_id = uuid4()
    elif change == "integration":
        h.tool.integration_id = uuid4()
    elif change == "failed":
        h.tool.status = "FAILED"
    elif change == "pending":
        h.tool.status = "RUNNING"
    elif change == "write-tool":
        h.tool.capability_version = "database.write/v1"
    elif change == "provider":
        h.tool.provider = "other"
    elif change == "missing-tool":
        h.evidence.tool_call_id = uuid4()
    elif change == "binding":
        h.evidence.metadata_json = {"binding_checksum": "other"}
    elif change == "evidence-ref":
        h.draft.evidence_refs = ("ev_other",)
    else:
        changes = {
            "table": {"table": "other.records"},
            "filters": {"filters": {"id": "row-2"}},
            "projection": {"columns": ["id"]},
            "truncated": {"truncated": True},
            "offset": {"offset": 1},
            "count": {"row_count": 1},
            "revision": {"row_hashes": ["sha256:" + "b" * 64]},
        }
        h.evidence.source_locator = {**h.evidence.source_locator, **changes[change]}
    with pytest.raises(ChangeProposalValidationError, match="exact read Evidence"):
        await verify(h)
