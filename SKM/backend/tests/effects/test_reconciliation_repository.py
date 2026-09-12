"""SQLite 実 transaction で要求/Outbox と原 owner を検証する。PG lock/FK は対象外。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from skillmind.db.models import EffectReconciliationRequest, OutboxMessage
from skillmind.effects.postgres_write import DatabaseWriteConflictError, DatabaseWriteReceipt
from skillmind.effects.reconciliation_domain import (
    EffectReconciliationDeniedError,
    EffectReconciliationObservation,
    EffectReconciliationReference,
    EffectReconciliationTarget,
)
from skillmind.effects.reconciliation_repository import (
    ReconciliationRequestRepository as Repository,
)
from skillmind.effects.reconciliation_requests import (
    ReconciliationRequestConflictError,
    reconciliation_command_checksum,
    reconciliation_kind,
    reconciliation_receipt_json,
)
from tests.effects.test_database_write import command
from tests.storage.test_object_effect import fixture as object_fixture


class SqlSession:
    """本物の SQL を repository の async port に適合し、SQLite 時刻を UTC に戻す。"""

    def __init__(self, session):
        """commit/rollback の責務は外側 transaction に残す。"""
        self.session = session

    async def scalar(self, query):
        """SQLite の DateTime 読取差だけを補正する。"""
        row = self.session.scalar(query)
        if isinstance(row, EffectReconciliationRequest):
            for name in (
                "created_at",
                "claimed_at",
                "lease_expires_at",
                "finished_at",
                "observed_at",
            ):
                value = getattr(row, name)
                if value is not None and value.tzinfo is None:
                    setattr(row, name, value.replace(tzinfo=UTC))
        return row

    def add(self, row):
        """ORM の実 add を使う。"""
        self.session.add(row)

    async def flush(self):
        """制約と rollback を実 DB で検証する。"""
        self.session.flush()


@pytest.fixture
def database():
    """PG 固有の regex/FK を除き、状態 CHECK と active index は実 SQL で試す。"""
    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata = sa.MetaData()
    for model in (EffectReconciliationRequest, OutboxMessage):
        source = model.__table__
        table = sa.Table(
            source.name,
            metadata,
            *(
                sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
                for c in source.columns
            ),
        )
        for constraint in source.constraints:
            if isinstance(constraint, sa.CheckConstraint) and " ~ " not in str(constraint.sqltext):
                table.append_constraint(sa.CheckConstraint(str(constraint.sqltext)))
        for index in source.indexes:
            sa.Index(
                index.name,
                *(table.c[c.name] for c in index.columns),
                unique=index.unique,
                sqlite_where=index.dialect_options["sqlite"].get("where"),
            )
    metadata.create_all(engine)

    @contextmanager
    def transaction():
        """各段階で別 Session を使い、identity map だけで復旧しない。"""
        with Session(engine, expire_on_commit=False) as session, session.begin():
            yield Repository(SqlSession(session)), session

    yield transaction
    engine.dispose()


@pytest.fixture
def original():
    """元書込は合成し、ここでは業務 Effect/Run table を作らない。"""
    target = EffectReconciliationTarget(
        uuid4(),
        uuid4(),
        "sha256:" + "1" * 64,
        "postgres",
        "postgres-receipt/v1",
        command(),
        "{}",
        uuid4(),
    )
    reference = EffectReconciliationReference(
        uuid4(),
        uuid4(),
        uuid4(),
        target.command.project_id,
        target.command.run_id,
        target.command.effect_id,
    )
    return dict(
        request_id=uuid4(),
        reference=reference,
        accepted_http_request_id=uuid4(),
        target=target,
        now=datetime.now(UTC),
    )


def observation(original, status="NOT_OBSERVED"):
    """原要求の一回の読取だけを模す。"""
    target = original["target"]
    receipt = None
    if status == "CONFIRMED":
        receipt = DatabaseWriteReceipt(None, {**target.command.key, **target.command.values}, True)
    return EffectReconciliationObservation(
        target.command.effect_id,
        reconciliation_kind(target),
        status,
        original["now"] + timedelta(seconds=1),
        reconciliation_command_checksum(target),
        receipt,
    )


async def test_accept_outbox_atomic_rollback_and_duplicate_active(database, original):
    """受理と ID-only dispatch は同時 commit/rollback し、二重 active を作らない。"""
    with pytest.raises(RuntimeError), database() as (repo, _):
        await repo.create(**original)
        raise RuntimeError("abort acceptance")
    with database() as (repo, session):
        assert await repo.find(original["request_id"]) is None
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxMessage)) == 0
        await repo.create(**original)
    with database() as (repo, session):
        row = await repo.require(original["request_id"])
        assert row.status == "QUEUED" and row.receipt_json is None
        assert (
            session.scalar(
                sa.text("SELECT receipt_json IS NULL FROM effect_reconciliation_requests")
            )
            == 1
        )
        outbox = session.scalar(sa.select(OutboxMessage))
        assert outbox.payload_json == {"request_id": str(row.id)}
        with pytest.raises(ReconciliationRequestConflictError):
            await repo.create(**{**original, "request_id": uuid4()})


@pytest.mark.parametrize("status", ["CONFIRMED", "NOT_OBSERVED", "CONFLICT"])
async def test_owner_completion_replay_and_new_read(database, original, status):
    """一度の claim/同じ観測 replay を保存し、終態後は新しい只読要求を許す。"""
    with database() as (repo, _):
        row = await repo.create(**original)
        owner = repo.claim(row, token="x" * 40, now=original["now"])
        assert repo.claim(row, token="y" * 40, now=original["now"]) is None
    observed = observation(original, status)
    now = original["now"] + timedelta(seconds=2)
    with database() as (repo, _):
        row = await repo.require(original["request_id"], lock=True)
        repo.finish(row, owner, observation=observed, target=original["target"], now=now)
    with database() as (repo, session):
        row = await repo.require(original["request_id"], lock=True)
        repo.finish(
            row,
            owner,
            observation=observed,
            target=original["target"],
            now=now + timedelta(minutes=5),
        )
        assert row.status == "SUCCEEDED" and row.finished_at == now
        assert row.observation_status == status
        assert not repo.fail(row, code="lookup_interrupted", now=now)
        with pytest.raises(ReconciliationRequestConflictError):
            repo.finish(
                row,
                owner,
                observation=replace(observed, observed_at=now),
                target=original["target"],
                now=now,
            )
        await repo.create(**{**original, "request_id": uuid4(), "now": now})
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxMessage)) == 2


@pytest.mark.parametrize("change", ["token", "session", "target", "expired", "failed"])
async def test_old_or_changed_owner_cannot_publish(database, original, change):
    """旧 Worker、異なる会話、期限後や回収後の保存を拒否する。"""
    with database() as (repo, _):
        row = await repo.create(**original)
        owner = repo.claim(row, token="x" * 40, now=original["now"])
    now = original["now"] + timedelta(seconds=2)
    if change == "token":
        owner = replace(owner, token="y" * 40)
    elif change == "session":
        owner = replace(
            owner,
            request=replace(
                owner.request, reference=replace(owner.request.reference, auth_session_id=uuid4())
            ),
        )
    elif change == "target":
        owner = replace(owner, request=replace(owner.request, target_checksum="sha256:" + "2" * 64))
    elif change == "expired":
        now += timedelta(seconds=60)
    with database() as (repo, _):
        row = await repo.require(original["request_id"], lock=True)
        if change == "failed":
            repo.fail(row, code="lookup_interrupted", now=now)
        with pytest.raises(EffectReconciliationDeniedError):
            repo.finish(
                row, owner, observation=observation(original), target=original["target"], now=now
            )
        assert row.observation_status is None and row.receipt_json is None


async def test_sql_rejects_null_observation_in_succeeded_state(database, original):
    """SQL CHECK の UNKNOWN 評価で成功行の観測欠落を通さない。"""
    with database() as (repo, _):
        row = await repo.create(**original)
        repo.claim(row, token="x" * 40, now=original["now"])
    with pytest.raises(IntegrityError), database() as (repo, session):
        row = await repo.require(original["request_id"])
        row.status = "SUCCEEDED"
        row.finished_at = row.observed_at = original["now"] + timedelta(seconds=1)
        session.flush()


@pytest.mark.parametrize("change", ["effect", "checksum", "kind", "unconfirmed", "before", "after"])
def test_receipt_rejects_other_identity_and_incomplete_facts(original, change):
    """保存経路も原 DB 回执の共通検証を通し、別要求や欠落列を拒否する。"""
    observed = observation(original, "CONFIRMED")
    changes = {
        "effect": {"effect_execution_id": uuid4()},
        "checksum": {"request_checksum": "sha256:" + "0" * 64},
        "kind": {"kind": "DOCUMENT_OBJECT"},
        "unconfirmed": {"status": "NOT_OBSERVED"},
        "before": {"receipt": replace(observed.receipt, before={"id": "other"})},
        "after": {"receipt": replace(observed.receipt, after={})},
    }
    with pytest.raises((ValueError, DatabaseWriteConflictError)):
        reconciliation_receipt_json(replace(observed, **changes[change]), original["target"])


def test_object_receipt_preserves_original_identity(original):
    """object 確認は transaction 確認と分離し、公開完了は主張しない。"""
    from skillmind.storage.effect_write import ObjectWriteReceipt

    _, command, _ = object_fixture()
    target = replace(
        original["target"], command=command, provider="project-library", secret_reference_id=None
    )
    receipt = ObjectWriteReceipt(
        command.effect_id,
        command.request_checksum,
        command.object_key,
        command.content_checksum,
        len(command.content),
        command.content_type,
        '"opaque"',
        None,
    )
    observed = EffectReconciliationObservation(
        command.effect_id,
        "DOCUMENT_OBJECT",
        "CONFIRMED",
        original["now"],
        command.request_checksum,
        receipt,
    )
    result = reconciliation_receipt_json(observed, target)
    assert result["effect_id"] == str(command.effect_id)
    with pytest.raises(ValueError):
        reconciliation_receipt_json(
            replace(observed, receipt=replace(receipt, effect_id=uuid4())), target
        )
