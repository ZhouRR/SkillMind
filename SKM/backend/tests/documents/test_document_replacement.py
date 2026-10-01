"""同名更新の競合・原 byte 保持・公開 rollback を実 SQLite transaction で確認する。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from skillmind.db.models import ProjectDocument
from skillmind.documents.domain import DocumentConflictError, UploadDocumentCommand
from skillmind.documents.effect_repository import DocumentEffectRepository
from skillmind.documents.file_state import observe_file_state
from skillmind.documents.repository import DocumentRepository
from tests.documents.test_document_effect_repository import database as database


async def original(db):
    """同名の既存文書と、その時点の公開版を作る。"""
    row = ProjectDocument(
        id=uuid4(),
        project_id=db.command.project_id,
        folder="results/review",
        name="source.md",
        storage_key="original/key",
        size=3,
        mime="text/markdown",
        checksum="sha256:" + "a" * 64,
        uploaded_by=db.actor,
        created_at=db.now,
    )
    with db.transaction() as session:
        session.add(row)
    with db.transaction() as session:
        state = await observe_file_state(
            session, project_id=row.project_id, path="results/review/source.md"
        )
    return row, state["revision"]


async def verified(db, revision):
    """送信・回読済みまで commit し、公開前の障害を独立して検証する。"""
    with db.transaction() as session:
        await db.reserve(session, expected_revision=revision)
        repo = DocumentEffectRepository(session)
        await repo.start_once(db.command, owner_id=uuid4(), now=db.now)
        await repo.record_verified(db.command, db.receipt(), now=db.now)


async def test_replacement_publishes_once_and_keeps_original_identity(database):
    db = database
    old, revision = await original(db)
    await verified(db, revision)
    with db.transaction() as session:
        new = await DocumentEffectRepository(session).publish(db.command, now=db.now)
    with db.transaction() as session:
        preserved = await session.get(ProjectDocument, old.id)
        assert preserved.deleted_at is not None
        assert preserved.storage_key == old.storage_key and preserved.checksum == old.checksum
        assert new.document_id != old.id
        assert (await session.get(ProjectDocument, new.document_id)).deleted_at is None
        assert await DocumentEffectRepository(session).publish(db.command, now=db.now) == new


@pytest.mark.parametrize("change", ["rename", "trash", "replace"])
async def test_changed_original_stops_publication_without_overwriting(database, change):
    db = database
    old, revision = await original(db)
    await verified(db, revision)
    with db.transaction() as session:
        row = await session.get(ProjectDocument, old.id)
        if change == "rename":
            row.name = "renamed.md"
        elif change == "trash":
            row.deleted_at = db.now
        else:
            row.checksum = "sha256:" + "b" * 64
    with pytest.raises(DocumentConflictError), db.transaction() as session:
        await DocumentEffectRepository(session).publish(db.command, now=db.now)
    with db.transaction() as session:
        assert (await DocumentEffectRepository(session).require(db.command)).state == "VERIFIED"
        assert (await session.get(ProjectDocument, old.id)).storage_key == old.storage_key


async def test_failed_publication_restores_original_visibility(database):
    db = database
    old, revision = await original(db)
    await verified(db, revision)
    with pytest.raises(PermissionError), db.transaction() as session:
        await DocumentEffectRepository(session).publish(db.command, now=db.now)
        await session.flush()
        raise PermissionError("Synthetic revocation")
    with db.transaction() as session:
        assert (await session.get(ProjectDocument, old.id)).deleted_at is None
        assert (await DocumentEffectRepository(session).require(db.command)).state == "VERIFIED"


async def test_human_upload_publication_rolls_back_replacement_atomically(database):
    """画面更新も実 SQL rollback で旧可視性を戻し、凍結 ID を書き換えない。"""
    db = database
    old, _ = await original(db)
    command = UploadDocumentCommand(
        project_id=old.project_id, document_id=uuid4(), folder=old.folder, name=old.name,
        storage_key="new/key", storage_namespace=db.command.namespace,
        upload_intent_id=uuid4(), size=7, mime="text/markdown", checksum="sha256:" + "b" * 64,
        uploaded_by=db.actor, replaces_document_id=old.id, expected_checksum=old.checksum,
    )
    with pytest.raises(RuntimeError), db.transaction() as session:
        await DocumentRepository(session).create(command)
        await session.flush()
        raise RuntimeError("Synthetic publication rollback")
    with db.transaction() as session:
        assert (await session.get(ProjectDocument, old.id)).deleted_at is None
        assert await session.get(ProjectDocument, command.document_id) is None
        new = await DocumentRepository(session).create(command)
    with db.transaction() as session:
        previous = await session.get(ProjectDocument, old.id)
        assert previous.deleted_at is not None and previous.storage_key == "original/key"
        assert new.document_id != old.id
