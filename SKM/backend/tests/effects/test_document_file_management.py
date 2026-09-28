"""画面と共通の目录変更を原 Effect 回执へ接続し、再送・競合・rollback を検証する。"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from skillmind.db.models import DocumentMutationReceipt, ProjectDocument
from skillmind.documents.domain import DocumentConflictError
from skillmind.documents.file_state import FOLDER_OPERATIONS, observe_file_state
from skillmind.effects.document_management import (
    DocumentManagementCommand,
    apply_document_management,
    lookup_management_receipt,
    mutation_checksum,
)
from tests.documents.test_document_management import add_document, apply, change
from tests.documents.test_document_management import db as db
from tests.effects.database_fixtures import database_execution


async def fixture(db, operation="MOVE"):
    """実 FK を持つ原 Effect と現在版を作り、認可 lock 自体は既存回帰へ委ねる。"""
    row = add_document(db)
    effect = db.seed("effect_executions")
    execution = replace(
        database_execution(),
        effect_execution_id=effect["id"],
        project_id=row.project_id,
        run_id=effect["run_id"],
        operation=operation,
    )
    path = "specs" if operation in FOLDER_OPERATIONS else "specs/source.md"
    with db.transaction() as port:
        state = await observe_file_state(
            port, project_id=row.project_id, path=path, folder=operation in FOLDER_OPERATIONS
        )
    payload = {"path": path, "expected_revision": state["revision"], "operation": operation}
    if operation == "MOVE":
        payload["destination"] = "output/renamed.md"
    elif operation == "MOVE_FOLDER":
        payload["destination"] = "output"
    return row, execution, payload


@pytest.mark.parametrize("operation", ["MOVE", "MOVE_FOLDER", "TRASH"])
async def test_committed_receipt_replays_without_reapplying_current_file(db, operation):
    row, execution, payload = await fixture(db, operation)
    with db.transaction() as port:
        result = await apply_document_management(port, execution, payload, actor_id=uuid4())
    assert not result.replayed
    with db.transaction() as port:
        current = await port.get(ProjectDocument, row.id)
        current.name = "later-edit.md"
    with db.transaction() as port:
        replay = await apply_document_management(port, execution, payload, actor_id=uuid4())
        assert replay.replayed and replay.after == result.after
        assert (await port.get(ProjectDocument, row.id)).name == "later-edit.md"
        command = DocumentManagementCommand(
            execution.effect_execution_id,
            execution.project_id,
            execution.run_id,
            mutation_checksum(execution, payload),
        )
        receipt = await lookup_management_receipt(port, command)
        assert receipt.result["after"] == result.after.content["document"]
        assert await lookup_management_receipt(port, replace(command, run_id=uuid4())) is None


async def test_screen_move_after_observation_refuses_agent_change(db):
    row, execution, payload = await fixture(db)
    await apply(db, "MOVE", [change(row, "ui", "renamed.md")])
    with pytest.raises(DocumentConflictError), db.transaction() as port:
        await apply_document_management(port, execution, payload, actor_id=uuid4())
    with db.transaction() as port:
        assert await port.scalar(select(DocumentMutationReceipt)) is None
        assert (await port.get(ProjectDocument, row.id)).folder == "ui"


async def test_metadata_and_receipt_rollback_together(db):
    row, execution, payload = await fixture(db)
    with pytest.raises(PermissionError), db.transaction() as port:
        await apply_document_management(port, execution, payload, actor_id=uuid4())
        await port.flush()
        raise PermissionError("Synthetic revoked authorization")
    with db.transaction() as port:
        assert await port.scalar(select(DocumentMutationReceipt)) is None
        assert (await port.get(ProjectDocument, row.id)).folder == "specs"


async def test_folder_revision_detects_new_child_and_no_partial_move(db):
    row, execution, payload = await fixture(db, "MOVE_FOLDER")
    add_document(db, "new.md", folder="specs/child")
    with pytest.raises(DocumentConflictError), db.transaction() as port:
        await apply_document_management(port, execution, payload, actor_id=uuid4())
    with db.transaction() as port:
        assert (await port.get(ProjectDocument, row.id)).folder == "specs"


@pytest.mark.parametrize("operation", ["CREATE_FOLDER", "DELETE_FOLDER", "RESTORE"])
async def test_remaining_file_operations_commit_exact_metadata(db, operation):
    """空目录作成・削除と原 ID の復元も、共有画面正本に反映する。"""
    row, execution, _ = await fixture(db, operation)
    if operation == "DELETE_FOLDER":
        await apply(db, "CREATE_FOLDER", target="empty")
    elif operation == "RESTORE":
        await apply(db, "TRASH", [change(row, row.folder, row.name)])
    path = "empty" if operation in FOLDER_OPERATIONS else "specs/source.md"
    with db.transaction() as port:
        observed = await observe_file_state(
            port,
            project_id=row.project_id,
            path=path,
            folder=operation in FOLDER_OPERATIONS,
            trashed=operation == "RESTORE",
            document_id=row.id,
        )
        result = await apply_document_management(
            port,
            execution,
            {
                "path": path,
                "expected_revision": "absent"
                if operation == "CREATE_FOLDER"
                else observed["revision"],
                "operation": operation,
                **({"document_id": str(row.id)} if operation == "RESTORE" else {}),
            },
            actor_id=uuid4(),
        )
    after = result.after.content["document"]
    if operation == "RESTORE":
        assert after["document"]["document_id"] == str(row.id)
        assert after["document"]["trashed"] is False
    else:
        assert after["exists"] is (operation == "CREATE_FOLDER")
