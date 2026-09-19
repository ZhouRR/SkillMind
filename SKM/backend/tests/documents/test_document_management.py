"""改名・移動・回収箱が内容 identity と原参照を維持することを検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from skillmind.db import models as m
from skillmind.documents.domain import DocumentConflictError, DocumentNotFoundError
from skillmind.documents.management import DocumentManagementRepository, path_conflicts
from skillmind.documents.repository import DocumentRepository
from tests.documents.management_sql import SqlDatabase


@pytest.fixture
def db():
    """各ケースを独立した SQL DB で実行する。"""
    value = SqlDatabase()
    value.seed("projects")
    yield value
    value.engine.dispose()


def add_document(db, name="source.md", folder="specs", **values):
    """外部 blob を触らず、保存済み ID/hash/key の維持を確認する。"""
    row = m.ProjectDocument(
        id=values.pop("id", uuid4()),
        project_id=db.rows["projects"]["id"],
        folder=folder,
        name=name,
        storage_key=f"objects/{uuid4()}",
        size=10,
        mime="text/markdown",
        checksum="sha256:" + "a" * 64,
        uploaded_by=uuid4(),
        created_at=datetime.now(UTC),
        **values,
    )
    with db.transaction() as port:
        port.add(row)
    return row


async def apply(db, action, changes=None, source=None, target=None):
    """同じ transaction 内で検証・flush・commit を通す。"""
    with db.transaction() as port:
        await DocumentManagementRepository(port).apply(
            db.rows["projects"]["id"], action, changes or [], source, target, actor_id=uuid4()
        )


def change(row, folder, name):
    """原 path を条件とする変更を作る。"""
    return dict(
        document_id=row.id,
        expected_folder=row.folder,
        expected_name=row.name,
        folder=folder,
        name=name,
    )


async def test_move_keeps_bytes_identity_and_rejects_stale_or_cross_project(db):
    row = add_document(db)
    await apply(db, "MOVE", [change(row, "new", "renamed.md")])
    with db.transaction() as port:
        updated = await port.get(m.ProjectDocument, row.id)
        assert (updated.folder, updated.name) == ("new", "renamed.md")
        assert (updated.id, updated.checksum, updated.storage_key) == (
            row.id,
            row.checksum,
            row.storage_key,
        )
    with pytest.raises(DocumentConflictError):
        await apply(db, "MOVE", [change(row, "", "stale.md")])
    with pytest.raises(DocumentNotFoundError):
        await apply(db, "MOVE", [{**change(row, "", "x"), "document_id": uuid4()}])


async def test_bulk_collision_rolls_back_all_documents(db):
    left, right = add_document(db, "a.md"), add_document(db, "b.md")
    with pytest.raises(DocumentConflictError):
        await apply(
            db, "MOVE", [change(left, "target", "same.md"), change(right, "target", "same.md")]
        )
    with db.transaction() as port:
        assert (await port.get(m.ProjectDocument, left.id)).folder == "specs"
        assert (await port.get(m.ProjectDocument, right.id)).folder == "specs"


async def test_empty_folder_subtree_move_and_file_directory_conflicts(db):
    await apply(db, "CREATE_FOLDER", target="specs/empty/deep")
    row = add_document(db, folder="specs/child")
    sibling = add_document(db, folder="specs-other")
    await apply(db, "MOVE_FOLDER", source="specs", target="review")
    with db.transaction() as port:
        assert (await port.get(m.ProjectDocument, row.id)).folder == "review/child"
        assert (await port.get(m.ProjectDocument, sibling.id)).folder == "specs-other"
        assert "review/empty/deep" in await DocumentManagementRepository(port).folders(
            row.project_id
        )
        assert await path_conflicts(port, row.project_id, "review", "empty")
        assert await path_conflicts(port, row.project_id, "review/child/source.md", "nested.md")
    with pytest.raises(DocumentConflictError):
        await apply(db, "MOVE_FOLDER", source="review", target="review/child")
    with pytest.raises(DocumentConflictError):
        await apply(db, "DELETE_FOLDER", source="review")
    await apply(db, "DELETE_FOLDER", source="review/empty")


async def test_recycle_path_reuse_and_restore_conflict(db):
    original = add_document(db)
    await apply(db, "TRASH", [change(original, original.folder, original.name)])
    with db.transaction() as port:
        repository = DocumentRepository(port)
        assert not await repository.list_for_project(original.project_id)
        assert len(await repository.list_for_project(original.project_id, trashed=True)) == 1
        assert (await port.get(m.ProjectDocument, original.id)).deleted_by is not None
    replacement = add_document(db)
    with pytest.raises(DocumentConflictError):
        await apply(db, "RESTORE", [change(original, original.folder, original.name)])
    await apply(db, "MOVE", [change(replacement, "", "other.md")])
    await apply(db, "RESTORE", [change(original, original.folder, original.name)])
    with db.transaction() as port:
        assert len(await DocumentRepository(port).list_for_project(original.project_id)) == 2
        assert (await port.get(m.ProjectDocument, original.id)).deleted_at is None


async def test_bulk_restore_conflict_does_not_restore_first_document(db):
    first, second = add_document(db, "first.md"), add_document(db, "second.md")
    await apply(db, "TRASH", [change(d, d.folder, d.name) for d in (first, second)])
    add_document(db, "second.md")
    with pytest.raises(DocumentConflictError):
        await apply(db, "RESTORE", [change(d, d.folder, d.name) for d in (first, second)])
    with db.transaction() as port:
        assert all(
            d.deleted_at
            for d in await port.scalars(
                select(m.ProjectDocument).where(m.ProjectDocument.id.in_([first.id, second.id]))
            )
        )


async def test_pending_upload_prevents_folder_relocation_or_file_ancestor(db):
    """受付済み upload の保存先を目录操作で消したり file と衝突させない。"""
    await apply(db, "CREATE_FOLDER", target="incoming")
    db.seed(
        "document_upload_intents",
        project_id=db.rows["projects"]["id"],
        state="PENDING",
        folder="incoming",
        name="new.md",
        publication_closed_at=None,
    )
    with pytest.raises(DocumentConflictError):
        await apply(db, "MOVE_FOLDER", source="incoming", target="elsewhere")
    with pytest.raises(DocumentConflictError):
        await apply(db, "DELETE_FOLDER", source="incoming")
    await apply(db, "CREATE_FOLDER", target="empty")
    with pytest.raises(DocumentConflictError):
        await apply(db, "MOVE_FOLDER", source="empty", target="incoming/new.md")
    row = add_document(db)
    with pytest.raises(DocumentConflictError):
        await apply(db, "MOVE", [change(row, "", "incoming")])
    with pytest.raises(DocumentConflictError):
        await apply(db, "CREATE_FOLDER", target="incoming/new.md/child")
