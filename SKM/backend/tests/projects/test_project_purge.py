"""帰档 Project の一括削除を実 SQLite の FK/rollback と共有 storage で検証する。"""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from skillmind.db import models as m
from skillmind.documents.domain import DocumentCleanupActor
from skillmind.projects.domain import ProjectDeleteBlockedError, ProjectVersionConflictError
from skillmind.projects.purge import purge_project
from skillmind.storage import InMemoryFileStorage
from tests.documents.management_sql import SqlDatabase


@pytest.fixture
def project_db():
    """終了済み Run、接続設定、調度、所属監査を持つ Project を用意する。"""
    db = SqlDatabase()
    project = db.seed("projects", status="ARCHIVED", key="purge-target")
    db.seed("users", preferred_project_id=project["id"])
    db.seed("auth_sessions")
    db.seed("runs", project_id=project["id"], status="SUCCEEDED")
    db.seed("run_segments", status="SUCCEEDED")
    db.seed("run_attempts", status="SUCCEEDED")
    db.seed("agent_sessions", status="CLOSED")
    db.seed("effect_executions", status="APPLIED")
    for table in (
        "run_skill_snapshots",
        "run_results",
        "evaluations",
        "run_events",
        "run_input_snapshots",
        "evidence",
        "task_schedules",
        "project_members",
        "project_skill_versions",
        "managed_secret_material",
        "project_compositions",
    ):
        db.seed(table)
    db.seed(
        "project_member_events",
        project_id=project["id"],
        member_id=db.rows["project_members"]["id"],
    )
    yield db
    db.engine.dispose()


def actor(db):
    """認可済み transaction の下流へ渡す合成実施者。"""
    return DocumentCleanupActor(
        organization_id=db.rows["organizations"]["id"],
        actor_id=db.rows["users"]["id"],
        request_id=uuid4(),
        session_id=db.rows["auth_sessions"]["id"],
    )


async def test_purge_removes_owned_graph_keeps_audits_skills_and_releases_key(project_db):
    db = project_db
    project_id = db.rows["projects"]["id"]
    with db.transaction() as port:
        project = await port.get(m.Project, project_id)
        assert (
            await purge_project(
                port, project=project, expected_row_version=1, actor=actor(db), storage=None
            )
            == []
        )
    with db.transaction() as port:
        assert await port.get(m.Project, project_id) is None
        for model in (
            m.Run,
            m.RunSkillSnapshot,
            m.TaskSchedule,
            m.ProjectMember,
            m.ResourceBinding,
            m.ManagedSecretMaterial,
            m.Integration,
            m.SecretReference,
            m.ProjectSkillVersion,
            m.ProjectComposition,
        ):
            assert await port.scalar(select(model.id)) is None
        assert await port.scalar(select(m.ProjectMemberEvent.id))
        assert await port.scalar(select(m.RunDeletionAudit.id))
        audit = await port.scalar(select(m.ProjectDeletionAudit))
        assert audit.project_id == project_id and audit.run_count == 1
        assert audit.organization_id == db.rows["organizations"]["id"]
        assert (await port.get(m.User, db.rows["users"]["id"])).preferred_project_id is None
        assert await port.get(m.SkillVersion, db.rows["skill_versions"]["id"]) is not None
        port.add(m.Project(**{**db.rows["projects"], "id": uuid4()}))


@pytest.mark.parametrize(
    ("table", "status"),
    [
        ("runs", "RUNNING"),
        ("run_attempts", "RUNNING"),
        ("agent_sessions", "ACTIVE"),
        ("effect_executions", "VERIFICATION_FAILED"),
    ],
)
async def test_active_or_uncertain_execution_keeps_all_rows(project_db, table, status):
    db = project_db
    model = next(
        mapper.class_ for mapper in m.Base.registry.mappers if mapper.class_.__tablename__ == table
    )
    with db.transaction() as port:
        (await port.get(model, db.rows[table]["id"])).status = status
    with pytest.raises(ProjectDeleteBlockedError), db.transaction() as port:
        await purge_project(
            port,
            project=await port.get(m.Project, db.rows["projects"]["id"]),
            expected_row_version=1,
            actor=actor(db),
            storage=None,
        )
    with db.transaction() as port:
        assert await port.scalar(select(m.Project.id))
        assert await port.scalar(select(m.TaskSchedule.id))
        assert await port.scalar(select(m.ProjectDeletionAudit.id)) is None


@pytest.mark.parametrize(
    "table", ["document_upload_intents", "document_effect_uploads", "task_schedule_occurrences"]
)
async def test_pending_upload_or_claim_is_not_discarded(project_db, table):
    db = project_db
    field = "status" if table == "task_schedule_occurrences" else "state"
    db.seed(table, project_id=db.rows["projects"]["id"], **{field: "PENDING"})
    with pytest.raises(ProjectDeleteBlockedError), db.transaction() as port:
        await purge_project(
            port,
            project=await port.get(m.Project, db.rows["projects"]["id"]),
            expected_row_version=1,
            actor=actor(db),
            storage=None,
        )
    with db.transaction() as port:
        assert await port.scalar(select(m.Project.id))
        assert await port.scalar(select(m.Run.id))


@pytest.mark.parametrize("version", [0, 2])
async def test_archive_and_original_version_are_checked_first(project_db, version):
    db = project_db
    with (
        pytest.raises(ProjectDeleteBlockedError if version == 0 else ProjectVersionConflictError),
        db.transaction() as port,
    ):
        project = await port.get(m.Project, db.rows["projects"]["id"])
        if version == 0:
            project.status = "ACTIVE"
        await purge_project(
            port, project=project, expected_row_version=version or 1, actor=actor(db), storage=None
        )


async def test_document_cleanup_is_retained_and_storage_not_called_before_commit(project_db):
    db = project_db
    storage = InMemoryFileStorage()
    storage.delete = AsyncMock()
    namespace = storage.namespace
    identifier = uuid4()
    db.seed(
        "project_documents",
        id=identifier,
        project_id=db.rows["projects"]["id"],
        upload_intent_id=None,
        effect_upload_id=None,
        folder="",
        name="input.md",
        checksum="sha256:" + "a" * 64,
        storage_key=f"objects/{identifier}",
        storage_namespace_id=namespace.namespace_id,
        storage_descriptor_checksum=namespace.descriptor_checksum,
        storage_is_durable=namespace.durable,
    )
    with db.transaction() as port:
        document = await port.get(m.ProjectDocument, identifier)
        # SQLite が失う timezone だけを adapter 境界で復元する。
        document.created_at = document.created_at.replace(tzinfo=UTC)
        references = await purge_project(
            port,
            project=await port.get(m.Project, db.rows["projects"]["id"]),
            expected_row_version=1,
            actor=actor(db),
            storage=storage,
        )
    assert len(references) == 1
    storage.delete.assert_not_awaited()
    with db.transaction() as port:
        assert await port.scalar(select(m.ProjectDocument.id)) is None
        cleanup = await port.scalar(select(m.ProjectDocumentCleanup))
        assert cleanup.document_id == identifier
        assert cleanup.project_id == db.rows["projects"]["id"]


async def test_database_failure_rolls_back_all_deletions(project_db, monkeypatch):
    db = project_db
    from skillmind.projects import purge

    monkeypatch.setattr(purge, "purge_run", AsyncMock(side_effect=RuntimeError("synthetic")))
    with pytest.raises(RuntimeError, match="synthetic"), db.transaction() as port:
        await purge_project(
            port,
            project=await port.get(m.Project, db.rows["projects"]["id"]),
            expected_row_version=1,
            actor=actor(db),
            storage=None,
        )
    with db.transaction() as port:
        assert await port.scalar(select(m.TaskSchedule.id))
        assert await port.scalar(select(m.Run.id))
        assert await port.scalar(select(m.ProjectDeletionAudit.id)) is None


async def test_old_upload_cleanup_and_membership_audits_do_not_block_purge(project_db):
    """公開済み文書の削除履歴が残っていても Project 本体は完全削除できる。"""
    db = project_db
    project_id = db.rows["projects"]["id"]
    db.seed("document_upload_intents", project_id=project_id, state="PUBLISHED")
    db.seed("document_blob_cleanups", project_id=project_id)
    with db.transaction() as port:
        await purge_project(port, project=await port.get(m.Project, project_id),
                            expected_row_version=1, actor=actor(db), storage=None)
    with db.transaction() as port:
        assert await port.get(m.Project, project_id) is None
        assert await port.scalar(select(m.ProjectDocumentUpload.id))
        assert await port.scalar(select(m.ProjectDocumentCleanup.id))
        assert await port.scalar(select(m.ProjectMemberEvent.id))


async def test_purge_does_not_remove_another_projects_content(project_db):
    """同組織の他 Project とその文書は明示削除範囲に含めない。"""
    db = project_db
    other_id, document_id = uuid4(), uuid4()
    with db.transaction() as port:
        port.add(m.Project(**{**db.rows["projects"], "id": other_id, "key": "other"}))
        await port.flush()
    db.seed("project_documents", id=document_id, project_id=other_id,
            upload_intent_id=None, effect_upload_id=None)
    with db.transaction() as port:
        await purge_project(port, project=await port.get(m.Project, db.rows["projects"]["id"]),
                            expected_row_version=1, actor=actor(db), storage=None)
    with db.transaction() as port:
        assert await port.get(m.Project, other_id)
        assert await port.get(m.ProjectDocument, document_id)
