"""履歴と成果の回収・復元が一つの transaction で扱われることを検証する。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from skillmind.db import models as m
from skillmind.documents.domain import DocumentConflictError, DocumentInUseError
from skillmind.runs import history_deletion as history
from tests.documents.test_document_management import add_document
from tests.runs import test_history_purge


@pytest.fixture
def db():
    """同じ完了済み FK graph を回収・復元にも使用する。"""
    yield from test_history_purge.db.__wrapped__()


@pytest.fixture
def history_service(db, monkeypatch):
    """認可 gate の後段に実 SQL を接続する。認証自体は既存 API/service 回帰が担当する。"""
    access = SimpleNamespace(actor=SimpleNamespace(user_id=uuid4()))
    locked = SimpleNamespace(actor=SimpleNamespace())
    monkeypatch.setattr(history, "validate_user_access", Mock())
    monkeypatch.setattr(history, "authorize_user_access", Mock())
    monkeypatch.setattr(history.UserRepository, "lock_users", AsyncMock(return_value=locked))
    monkeypatch.setattr(
        history.ProjectRepository, "lock_write_access", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(history.ProjectRepository, "require_active_write_access", Mock())
    monkeypatch.setattr(history.ProjectRepository, "require_read_access", Mock())
    references = AsyncMock()
    monkeypatch.setattr(history.DocumentReferenceRepository, "require_unreferenced", references)

    @asynccontextmanager
    async def begin():
        yield

    @asynccontextmanager
    async def factory():
        with db.transaction() as port:
            port.begin = begin
            yield port

    async def manage(action):
        return await history.manage_history(
            factory,
            project_id=db.rows["projects"]["id"],
            run_id=db.rows["runs"]["id"],
            access=access,
            action=action,
            include_outputs=True,
        )

    with db.transaction() as port:
        # fixture の Run は回収済みなので、通常一覧から始める。
        port.session.get(m.Run, db.rows["runs"]["id"]).deleted_at = None
    identifier = uuid4()
    receipt = db.seed("document_effect_uploads", state="PUBLISHED", document_id=identifier)
    generated = add_document(db, id=identifier, name="generated.md", effect_upload_id=receipt["id"])
    original = add_document(db, name="original.md")
    return manage, generated, original, references


async def test_run_restore_collision_rolls_back_run_and_outputs(db, history_service):
    """同名の新規文書がある復元は履歴も成果も回収済みのままにする。"""
    manage, generated, original, _ = history_service
    await manage("TRASH")
    replacement = add_document(db, name=generated.name, folder=generated.folder)
    with pytest.raises(DocumentConflictError):
        await manage("RESTORE")
    with db.transaction() as port:
        assert (await port.get(m.Run, db.rows["runs"]["id"])).deleted_at is not None
        output = await port.get(m.ProjectDocument, generated.id)
        assert output.deleted_at is not None and output.deleted_by_run_id == db.rows["runs"]["id"]
        assert (await port.get(m.ProjectDocument, original.id)).deleted_at is None
        await port.delete(await port.get(m.ProjectDocument, replacement.id))
    await manage("RESTORE")
    with db.transaction() as port:
        assert (await port.get(m.Run, db.rows["runs"]["id"])).deleted_at is None
        output = await port.get(m.ProjectDocument, generated.id)
        assert output.deleted_at is None and output.deleted_by_run_id is None


async def test_shared_output_stays_visible_when_run_is_trashed(db, history_service):
    """別履歴の入力に使われる成果は関連成果の一括削除でも保全する。"""
    manage, generated, _, references = history_service
    references.side_effect = DocumentInUseError("Retained input")
    result = await manage("TRASH")
    assert result["protected_output_count"] == 1
    with db.transaction() as port:
        assert (await port.get(m.Run, db.rows["runs"]["id"])).deleted_at is not None
        assert (await port.get(m.ProjectDocument, generated.id)).deleted_at is None
