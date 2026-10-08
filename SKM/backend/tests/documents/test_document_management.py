"""改名・移動・回収箱が内容 identity と原参照を維持することを検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from skillmind.db import models as m
from skillmind.documents.domain import (
    DocumentConflictError,
    DocumentInUseError,
    DocumentNotFoundError,
    DocumentReferencesUnavailableError,
)
from skillmind.documents.management import DocumentManagementRepository, path_conflicts
from skillmind.documents.reference_repository import DocumentReferenceRepository
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


async def test_trash_preserves_original_document_despite_opaque_retained_history(db):
    """旧履歴を解析できなくても回収・復元は可能。原バイトと完全削除の参照制約は保つ。"""
    original = add_document(db)
    # 古い形式の未検証履歴を意図的に保持し、回収のために書換えたり無視したりしない。
    retained = db.seed("runs", project_id=original.project_id, status="SUCCEEDED")
    await apply(db, "TRASH", [change(original, original.folder, original.name)])
    with db.transaction() as port:
        repository = DocumentRepository(port)
        assert not await repository.list_for_project(original.project_id)
        saved, reference = await repository.get_for_download(
            project_id=original.project_id, document_id=original.id,
        )
        assert (saved.document_id, saved.checksum, saved.size, reference.key) == (
            original.id, original.checksum, original.size, original.storage_key,
        )
        assert (await port.get(m.Run, retained["id"])).selected_sources_json == retained[
            "selected_sources_json"
        ]
        with pytest.raises(DocumentReferencesUnavailableError):
            await DocumentReferenceRepository(port).require_unreferenced(
                project_id=original.project_id, document_id=original.id,
            )
    await apply(db, "RESTORE", [change(original, original.folder, original.name)])
    with db.transaction() as port:
        assert (await DocumentRepository(port).get(
            project_id=original.project_id, document_id=original.id,
        )).checksum == original.checksum


@pytest.mark.parametrize("run_status,effect_status,allowed", [
    ("SUCCEEDED", "APPLIED", True),
    ("RUNNING", "APPLIED", False),
    ("SUCCEEDED", "VERIFICATION_FAILED", False),
])
async def test_generated_output_trash_keeps_original_operation_guard(
    db, run_status, effect_status, allowed,
):
    """終端成果だけを回収でき、稼働中や原操作の検証待ちは履歴参照と独立に保護する。"""
    run = db.seed("runs", project_id=db.rows["projects"]["id"], status=run_status)
    db.seed("agent_sessions", run_id=run["id"], status="IDLE")
    db.seed("run_attempts", run_id=run["id"], status="SUCCEEDED")
    effect = db.seed("effect_executions", run_id=run["id"], status=effect_status)
    upload = db.seed(
        "document_effect_uploads", run_id=run["id"], effect_id=effect["id"],
        project_id=db.rows["projects"]["id"], state="PUBLISHED", protocol_version=2,
    )
    document = add_document(db, id=upload["document_id"], effect_upload_id=upload["id"])
    if allowed:
        await apply(db, "TRASH", [change(document, document.folder, document.name)])
    else:
        with pytest.raises(DocumentInUseError):
            await apply(db, "TRASH", [change(document, document.folder, document.name)])
    with db.transaction() as port:
        saved = await port.get(m.ProjectDocument, document.id)
        assert (saved.deleted_at is not None) is allowed
        assert saved.effect_upload_id == upload["id"]
        assert saved.storage_key == document.storage_key


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
