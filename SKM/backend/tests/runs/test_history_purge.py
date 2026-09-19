"""完全削除の FK 順序、参照解除、未解決操作の保護を実 SQL で検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from skillmind.db import models as m
from skillmind.documents.domain import DocumentCleanupActor
from skillmind.runs.history_deletion import RunHistoryConflict, require_finished
from skillmind.runs.history_purge import purge_run
from tests.documents.management_sql import SqlDatabase


@pytest.fixture
def db():
    """一つの実行に属する FK graph と、無関係の Skill 版を保持する。"""
    value = SqlDatabase()
    project = value.seed("projects")
    value.seed("auth_sessions")
    value.seed("runs", project_id=project["id"], status="SUCCEEDED", deleted_at=datetime.now(UTC))
    value.seed("run_segments", status="SUCCEEDED")
    value.seed("run_attempts", status="SUCCEEDED")
    value.seed("agent_sessions", status="CLOSED", sdk_session_id=uuid4())
    value.seed("run_budget_accounts")
    value.seed("run_budget_reservations", status="SETTLED", invocation_id=uuid4())
    value.seed("effect_executions", status="APPLIED")
    for name in (
        "run_skill_snapshots",
        "run_results",
        "evaluations",
        "run_events",
        "run_input_snapshots",
        "agent_task_brief_snapshots",
        "user_interactions",
        "interaction_responses",
        "permission_decisions",
        "evidence",
        "run_budget_receipts",
        "run_budget_observations",
    ):
        value.seed(name)
    yield value
    value.engine.dispose()


def actor(db):
    """共有認可 gate の後段を検証する合成の削除実施者。"""
    return DocumentCleanupActor(
        organization_id=db.rows["organizations"]["id"],
        actor_id=db.rows["users"]["id"],
        request_id=uuid4(),
        session_id=db.rows["auth_sessions"]["id"],
    )


async def test_purge_releases_skill_references_and_keeps_only_deletion_audit(db):
    run_id = db.rows["runs"]["id"]
    skill_id = db.rows["skill_versions"]["id"]
    with db.transaction() as port:
        run = await port.get(m.Run, run_id)
        assert await purge_run(port, run, [], [], actor(db), False, None) == []
    with db.transaction() as port:
        assert await port.get(m.Run, run_id) is None
        assert (
            await port.scalar(
                select(m.RunSkillSnapshot.id).where(m.RunSkillSnapshot.run_id == run_id)
            )
            is None
        )
        assert (
            await port.scalar(select(m.ChangeProposal.id).where(m.ChangeProposal.run_id == run_id))
            is None
        )
        audit = await port.scalar(
            select(m.RunDeletionAudit).where(m.RunDeletionAudit.run_id == run_id)
        )
        assert audit.idempotency_key == db.rows["runs"]["idempotency_key"]
        assert await port.get(m.SkillVersion, skill_id) is not None


@pytest.mark.parametrize(
    "model,status",
    [
        (m.Run, "RUNNING"),
        (m.RunAttempt, "RUNNING"),
        (m.AgentSession, "ACTIVE"),
        (m.EffectExecution, "VERIFICATION_FAILED"),
        (m.RunBudgetReservation, "START_INTENT"),
    ],
)
async def test_purge_rejects_unfinished_or_unknown_operations(db, model, status):
    with db.transaction() as port:
        row = await port.get(model, db.rows[model.__tablename__]["id"])
        row.status = status
    with pytest.raises(RunHistoryConflict), db.transaction() as port:
        await purge_run(
            port, await port.get(m.Run, db.rows["runs"]["id"]), [], [], actor(db), False, None
        )
    with db.transaction() as port:
        assert await port.get(m.Run, db.rows["runs"]["id"]) is not None


async def test_purge_requires_recycle_bin(db):
    with db.transaction() as port:
        run = await port.get(m.Run, db.rows["runs"]["id"])
        run.deleted_at = None
        with pytest.raises(RunHistoryConflict):
            await purge_run(port, run, [], [], actor(db), False, None)


async def test_active_reconciliation_prevents_deletion(db):
    db.seed("effect_reconciliation_requests", status="RUNNING")
    with db.transaction() as port, pytest.raises(RunHistoryConflict):
        await require_finished(port, await port.get(m.Run, db.rows["runs"]["id"]))


@pytest.mark.parametrize("protected,include_outputs", [(False, True), (True, True), (False, False)])
async def test_purge_only_deletes_explicit_unshared_outputs(db, protected, include_outputs):
    """原入力・共有文書は残し、専有成果は清理要求を保存してから FK を除く。"""
    from skillmind.storage import InMemoryFileStorage

    storage = InMemoryFileStorage()
    namespace = storage.namespace
    doc_id = uuid4()
    receipt = db.seed(
        "document_effect_uploads",
        state="PUBLISHED",
        document_id=doc_id,
        project_id=db.rows["projects"]["id"],
        storage_namespace_id=namespace.namespace_id,
        storage_descriptor_checksum=namespace.descriptor_checksum,
        storage_is_durable=namespace.durable,
    )
    original_id = uuid4()
    with db.transaction() as port:
        for identifier, effect in ((doc_id, receipt["id"]), (original_id, None)):
            port.add(
                m.ProjectDocument(
                    id=identifier,
                    project_id=db.rows["projects"]["id"],
                    effect_upload_id=effect,
                    folder="results",
                    name=str(identifier) + ".md",
                    storage_key=f"objects/{identifier}",
                    storage_namespace_id=namespace.namespace_id,
                    storage_descriptor_checksum=namespace.descriptor_checksum,
                    storage_is_durable=namespace.durable,
                    size=1,
                    mime="text/markdown",
                    checksum="sha256:" + "a" * 64,
                    uploaded_by=db.rows["users"]["id"],
                    created_at=datetime.now(UTC),
                )
            )
    with db.transaction() as port:
        run = await port.get(m.Run, db.rows["runs"]["id"])
        document = await port.get(m.ProjectDocument, doc_id)
        # SQLite は timezone を保持しないので adapter 境界で元 UTC を復元する。
        document.created_at = document.created_at.replace(tzinfo=UTC)
        refs = await purge_run(
            port,
            run,
            [document],
            [doc_id] if protected else [],
            actor(db),
            include_outputs,
            storage,
        )
        assert len(refs) == (1 if include_outputs and not protected else 0)
    with db.transaction() as port:
        assert await port.get(m.ProjectDocument, original_id) is not None
        remaining = await port.get(m.ProjectDocument, doc_id)
        if include_outputs and not protected:
            assert remaining is None
            assert await port.scalar(
                select(m.ProjectDocumentCleanup.id).where(
                    m.ProjectDocumentCleanup.document_id == doc_id
                )
            )
        else:
            assert remaining.effect_upload_id is None
            assert remaining.storage_key == f"objects/{doc_id}"


async def test_deleted_request_key_cannot_start_the_same_operation_again(db):
    """完全削除しても遅延した元 POST が新しい業務実行にならない。"""
    from skillmind.runs.domain import CreateRunCommand, IdempotencyConflictError
    from skillmind.runs.repository import RunRepository

    run = db.rows["runs"]
    with db.transaction() as port:
        await purge_run(port, await port.get(m.Run, run["id"]), [], [], actor(db), False, None)
    command = CreateRunCommand(
        project_id=run["project_id"],
        task_id=run["task_id"],
        idempotency_key=run["idempotency_key"],
        input_json={},
        task_snapshot_json={},
        permission_snapshot_json={},
        selected_sources_json={},
        limits_snapshot_json={},
        trace_id="fixture",
    )
    with db.transaction() as port, pytest.raises(IdempotencyConflictError):
        await RunRepository(port).create_idempotent(command)
