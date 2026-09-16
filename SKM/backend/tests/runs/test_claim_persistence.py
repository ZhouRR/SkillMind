"""実 ORM と外部キーで claim の保存順と rollback を検証する。PG lock の証明ではない。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from skillmind.db.models import OutboxMessage, Run, RunAttempt, RunEvent, RunSegment
from skillmind.runs.repository import RunRepository
from sqlalchemy import Column, ForeignKey, MetaData, Table, create_engine, select
from sqlalchemy.orm import Session
from tests.runs.test_run_repository import create_queued_run, create_segment


class ClaimSession:
    """本番 ORM の flush/autoflush を変更せず非同期 repository に接続する。"""

    def __init__(self, session):
        """transaction の寿命は呼出元が所有する。"""
        self.session = session

    async def scalars(self, statement):
        """実 SQL で複数行を検索する。"""
        return self.session.scalars(statement)

    async def scalar(self, statement):
        """実 SQL で一値を検索する。"""
        return self.session.scalar(statement)

    def add(self, item):
        """本番 mapped object を追加する。"""
        self.session.add(item)

    def add_all(self, items):
        """本番 mapped object をまとめて追加する。"""
        self.session.add_all(items)

    async def flush(self):
        """commit せずに実際の INSERT を行う。"""
        self.session.flush()


@pytest.mark.parametrize("commit", [True, False])
async def test_claim_persists_attempt_before_referencing_events(commit):
    """初回 claim の外部キー整合性と、flush 後も全体 rollback 可能なことを検証する。"""
    engine = create_engine("sqlite:///:memory:")
    models = (Run, RunSegment, RunAttempt, RunEvent, OutboxMessage)
    names = {model.__tablename__ for model in models}
    metadata = MetaData()
    for model in models:
        Table(model.__tablename__, metadata, *[
            Column(column.name, column.type, *[
                ForeignKey(fk.target_fullname)
                for fk in column.foreign_keys
                if fk.target_fullname.split(".")[0] in names
            ], primary_key=column.primary_key, nullable=column.nullable)
            for column in model.__table__.columns
        ])
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        metadata.create_all(connection)
    try:
        with Session(engine, expire_on_commit=False) as session:
            run = create_queued_run()
            segment = create_segment(run)
            session.add(run)
            session.flush()
            session.add(segment)
            session.commit()
            repository = RunRepository(ClaimSession(session))
            claim = await repository.claim_for_execution(
                run.id, worker_id="worker", lease_token="fixture-token",
                lease_token_hash="a" * 64,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1), max_attempts=3,
            )
            # 検索時の autoflush でも commit 時と同じ外部キー制約を通す。
            events = session.scalars(select(RunEvent).order_by(RunEvent.sequence)).all()
            assert claim is not None
            assert [item.event_type for item in events] == ["SEGMENT_STARTED", "RUN_SNAPSHOT"]
            assert all(item.run_attempt_id == claim.run_attempt_id for item in events)
            assert session.get(RunAttempt, claim.run_attempt_id).status == "LEASED"
            assert await repository.claim_for_execution(
                run.id, worker_id="duplicate", lease_token="other",
                lease_token_hash="b" * 64,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1), max_attempts=3,
            ) is None
            if commit:
                session.commit()
            else:
                session.rollback()
            session.expire_all()
            assert session.get(Run, run.id).status == ("PREPARING" if commit else "QUEUED")
            assert len(session.scalars(select(RunAttempt)).all()) == int(commit)
            assert len(session.scalars(select(RunEvent)).all()) == 2 * int(commit)
            assert len(session.scalars(select(OutboxMessage)).all()) == 2 * int(commit)
    finally:
        engine.dispose()
