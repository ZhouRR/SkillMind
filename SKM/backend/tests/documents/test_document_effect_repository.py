"""成果予約/公開の実 SQL と rollback を SQLite で検証する。PG lock/FK の証明ではない。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, UniqueConstraint, create_engine, select
from sqlalchemy.orm import Session

from skillmind.artifacts.repository import ArtifactRepository
from skillmind.db.models import (
    ProjectDocument,
    ProjectDocumentCleanup,
    ProjectDocumentEffectUpload,
    ProjectDocumentUpload,
)
from skillmind.documents.domain import (
    DocumentConflictError,
    DocumentInUseError,
    DocumentUploadInvalidError,
)
from skillmind.documents.effect_repository import DocumentEffectRepository
from skillmind.documents.paths import document_effect_storage_key
from skillmind.documents.repository import DocumentRepository
from skillmind.documents.upload_repository import DocumentUploadRepository
from skillmind.storage.effect_write import ObjectWriteReceipt, build_object_write
from skillmind.storage.validation import UploadLimits, UploadRejectedError
from tests.storage.test_object_effect import fixture as object_fixture


class SqlSession:
    """本物の transaction を async repository の小さな port に適合する。"""

    def __init__(self, session):
        """SQLite のみで使う同期 session を保持する。"""
        self.session = session

    async def scalar(self, statement):
        """SQLite DateTime の timezone 脱落を test adapter で UTC に戻す。"""
        value = self.session.scalar(statement)
        if isinstance(value, ProjectDocumentEffectUpload):
            for name in ("created_at", "sent_at", "verified_at", "published_at"):
                timestamp = getattr(value, name)
                if timestamp is not None and timestamp.tzinfo is None:
                    setattr(value, name, timestamp.replace(tzinfo=UTC))
        return value

    async def get(self, model, identifier):
        """通常文書の取得/削除拒否にも同じ transaction を使う。"""
        return self.session.get(model, identifier)

    def add(self, row):
        """add 後は本物の flush/commit/rollback を Session に任せる。"""
        self.session.add(row)


@pytest.fixture
def database(monkeypatch):
    """本文保存は既存 Artifact port に固定し、新規 writer の SQL/transaction を試験する。"""
    engine = create_engine("sqlite://")
    metadata = MetaData()
    # PostgreSQL 専用 CHECK/FK はこの adapter に移植せず、migration の別検証で扱う。
    for model in (
        ProjectDocument,
        ProjectDocumentEffectUpload,
        ProjectDocumentUpload,
        ProjectDocumentCleanup,
    ):
        source = model.__table__
        table = Table(
            source.name,
            metadata,
            *(
                Column(
                    column.name,
                    column.type,
                    primary_key=column.primary_key,
                    nullable=column.nullable,
                )
                for column in source.columns
            ),
        )
        for constraint in source.constraints:
            if isinstance(constraint, UniqueConstraint):
                table.append_constraint(
                    UniqueConstraint(*(column.name for column in constraint.columns))
                )
    metadata.create_all(engine)
    _, _, arguments = object_fixture()
    arguments.update(
        object_key=document_effect_storage_key(
            arguments["project_id"], "results/review", "source.md"
        ),
        allowed_prefix=f"projects/{arguments['project_id']}/documents/effects/",
    )
    command = build_object_write(**arguments)
    monkeypatch.setattr(
        ArtifactRepository, "get_content", AsyncMock(return_value=arguments["artifact"])
    )
    db = Database(engine, command)
    try:
        yield db
    finally:
        engine.dispose()


class Database:
    """各段階を別 Session にして、再起動時にも保存済み原行からだけ復旧する。"""

    def __init__(self, engine, command):
        """合成の認可済み帰属・上限と原 byte を固定する。"""
        self.engine, self.command = engine, command
        self.organization, self.actor = uuid4(), uuid4()
        self.now = datetime.now(UTC)
        self.limits = UploadLimits(1_048_576, 10_000, frozenset({"text/markdown"}))

    @contextmanager
    def transaction(self):
        """例外時の SQL rollback と、commit 後の新 Session 読取を使用する。"""
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            yield SqlSession(session)

    async def reserve(self, session, **overrides):
        """呼出元の Org 認可 gate は別試験とし、予約引数だけを共有する。"""
        values = dict(
            organization_id=self.organization,
            actor_id=self.actor,
            folder="results/review",
            name="source.md",
            limits=self.limits,
            now=self.now,
        )
        return await DocumentEffectRepository(session).reserve(
            self.command, **{**values, **overrides}
        )

    def receipt(self):
        """条件 client が返す原 object の検証済み receipt を模す。"""
        command = self.command
        return ObjectWriteReceipt(
            command.effect_id,
            command.request_checksum,
            command.object_key,
            command.content_checksum,
            len(command.content),
            command.content_type,
            '"original"',
            "version-1",
        )


async def test_restart_preserves_path_one_send_quota_and_original_publication(database):
    """予約/送信/核対/公開を別 commit にし、占用と公開回执を一度だけ保存する。"""
    db = database
    with db.transaction() as session:
        original = await db.reserve(session)
        identifier = original.document_id
    with db.transaction() as session:
        assert (await db.reserve(session)).document_id == identifier
        assert await DocumentRepository(session).project_usage_bytes(db.command.project_id) == len(
            db.command.content
        )
        assert await DocumentUploadRepository(session).path_reserved(
            project_id=db.command.project_id, folder="results/review", name="source.md"
        )
        assert await DocumentEffectRepository(session).start_once(
            db.command, owner_id=uuid4(), now=db.now
        )
    with db.transaction() as session:
        assert not await DocumentEffectRepository(session).start_once(
            db.command, owner_id=uuid4(), now=db.now
        )
        await DocumentEffectRepository(session).record_verified(
            db.command, db.receipt(), now=db.now
        )
    with db.transaction() as session:
        document = await DocumentEffectRepository(session).publish(db.command, now=db.now)
        assert document.document_id == identifier
    with db.transaction() as session:
        assert await DocumentRepository(session).project_usage_bytes(db.command.project_id) == len(
            db.command.content
        )
        assert (
            await DocumentEffectRepository(session).publish(
                db.command, now=db.now + timedelta(days=1)
            )
            == document
        )
        current = await session.get(ProjectDocument, identifier)
        assert current.effect_upload_id is not None and current.upload_intent_id is None
        with pytest.raises(DocumentInUseError):
            await DocumentRepository(session).delete(
                project_id=db.command.project_id, document_id=identifier, cleanup_actor=None
            )


async def test_publication_rollback_keeps_verified_receipt_and_reservation(database):
    """目録 flush 後に最終認可が拒否した場合を rollback し、二重公開/占用解放しない。"""
    db = database
    with db.transaction() as session:
        row = await db.reserve(session)
        identifier = row.document_id
        repo = DocumentEffectRepository(session)
        await repo.start_once(db.command, owner_id=uuid4(), now=db.now)
        await repo.record_verified(db.command, db.receipt(), now=db.now)
    with pytest.raises(PermissionError), db.transaction() as session:
        await DocumentEffectRepository(session).publish(db.command, now=db.now)
        session.session.flush()
        raise PermissionError("synthetic revocation")
    with db.transaction() as session:
        assert await session.get(ProjectDocument, identifier) is None
        assert (await DocumentEffectRepository(session).require(db.command)).state == "VERIFIED"
        assert await DocumentRepository(session).project_usage_bytes(db.command.project_id) == len(
            db.command.content
        )


async def test_refused_quota_and_missing_artifact_leave_no_reservation(database, monkeypatch):
    """予約前の quota/原 Artifact 不一致で占用行を commit しない。"""
    db = database
    with pytest.raises(UploadRejectedError), db.transaction() as session:
        await db.reserve(session, limits=replace(db.limits, project_quota_bytes=1))
    monkeypatch.setattr(ArtifactRepository, "get_content", AsyncMock(return_value=None))
    with pytest.raises(DocumentUploadInvalidError), db.transaction() as session:
        await db.reserve(session)
    with db.transaction() as session:
        assert await session.scalar(select(ProjectDocumentEffectUpload)) is None


async def test_changed_actor_path_or_command_cannot_replace_original_reservation(database):
    """同じ Effect に新しい帰属・保存先・字節を混ぜない。"""
    db = database
    with db.transaction() as session:
        await db.reserve(session)
    with db.transaction() as session:
        with pytest.raises(DocumentUploadInvalidError):
            await db.reserve(session, actor_id=uuid4())
        with pytest.raises(DocumentUploadInvalidError):
            await db.reserve(session, folder="results/other")
        with pytest.raises(ValueError):
            await DocumentEffectRepository(session).require(replace(db.command, content=b"changed"))


async def test_unverified_or_other_effect_receipt_cannot_publish(database):
    """送信開始や同値だけでは公開できず、元 Effect の核対 receipt を必須にする。"""
    db = database
    with db.transaction() as session:
        await db.reserve(session)
    with db.transaction() as session:
        repo = DocumentEffectRepository(session)
        with pytest.raises(DocumentUploadInvalidError):
            await repo.publish(db.command, now=db.now)
        await repo.start_once(db.command, owner_id=uuid4(), now=db.now)
        with pytest.raises(DocumentUploadInvalidError):
            await repo.record_verified(
                db.command, replace(db.receipt(), effect_id=uuid4()), now=db.now
            )
        assert (await repo.require(db.command)).state == "SENT"


async def test_existing_document_path_is_not_overwritten(database):
    """先に存在する目録は元成果の identity として採用しない。"""
    db = database
    with db.transaction() as session:
        session.add(
            ProjectDocument(
                id=uuid4(),
                project_id=db.command.project_id,
                folder="results/review",
                name="source.md",
                storage_key="old/key",
                size=1,
                mime="text/plain",
                checksum="sha256:" + "a" * 64,
                uploaded_by=db.actor,
                created_at=db.now,
            )
        )
    with pytest.raises(DocumentConflictError), db.transaction() as session:
        await db.reserve(session)
