"""終了済み実行とその公開成果を回収箱へ移し、監査・外部操作は変更しない。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.db.models import (
    EffectExecution,
    McpDesktopLease,
    ProjectDocument,
    ProjectDocumentEffectUpload,
    Run,
    RunAttempt,
)
from skillmind.documents.domain import (
    DocumentCleanupActor,
    DocumentConflictError,
    DocumentInUseError,
    DocumentReferencesUnavailableError,
    DocumentStorageUnavailableError,
)
from skillmind.documents.reference_repository import DocumentReferenceRepository
from skillmind.projects.domain import ProjectNotFoundError
from skillmind.projects.repository import ProjectRepository
from skillmind.runs.domain import TERMINAL_RUN_STATUSES, RunNotFoundError, RunStatus
from skillmind.storage import FileStorage, FileStorageError
from skillmind.users.access import authorize_user_access, validate_user_access
from skillmind.users.domain import UserAccess
from skillmind.users.repository import UserRepository


class RunHistoryConflict(ValueError):
    """稼働・不明操作・参照競合のため削除または復元できない。"""


async def manage_history(
    factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    run_id: UUID,
    access: UserAccess,
    action: str,
    include_outputs: bool = False,
    storage: FileStorage | None = None,
) -> dict[str, Any]:
    """認可 gate と Run lock の内側で削除前確認・削除・復元を行う。"""
    validate_user_access(access)
    if action not in {"PREVIEW", "TRASH", "RESTORE", "PURGE"}:
        raise ValueError("Unsupported history action")
    references = []
    write = action != "PREVIEW"
    async with factory() as session, session.begin():
        users = await UserRepository(session).lock_users(
            access=access, target_id=None, include_target_sessions=False, read_only_actor=True
        )

        def authorize() -> None:
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=write)

        authorize()
        projects = ProjectRepository(session)
        try:
            project = await projects.lock_write_access(user=users.actor, project_id=project_id)
        except ProjectNotFoundError:
            authorize()
            raise
        authorize()
        if write:
            projects.require_active_write_access(project)
        else:
            projects.require_read_access(project)
        run = await session.scalar(
            select(Run).where(Run.id == run_id, Run.project_id == project_id).with_for_update()
        )
        authorize()
        if run is None:
            raise RunNotFoundError("Run is not available")
        documents = list(
            await session.scalars(
                select(ProjectDocument)
                .join(
                    ProjectDocumentEffectUpload,
                    ProjectDocumentEffectUpload.id == ProjectDocument.effect_upload_id,
                )
                .where(
                    ProjectDocument.project_id == project_id,
                    ProjectDocumentEffectUpload.run_id == run_id,
                )
                .with_for_update(of=ProjectDocument)
            )
        )
        protected = []
        for document in documents:
            try:
                await DocumentReferenceRepository(session).require_unreferenced(
                    project_id=project_id, document_id=document.id
                )
            except (DocumentInUseError, DocumentReferencesUnavailableError):
                protected.append(document.id)
        preview: dict[str, Any] = {
            "run_id": str(run_id),
            "deleted": run.deleted_at is not None,
            "output_count": len(documents),
            "protected_output_count": len(protected),
            "outputs": [
                {
                    "document_id": str(d.id),
                    "name": d.name,
                    "folder": d.folder,
                    "protected": d.id in protected,
                }
                for d in documents
            ],
        }
        if action == "PREVIEW":
            authorize()
            return preview
        try:
            if action == "TRASH":
                await require_finished(session, run)
                now = datetime.now(UTC)
                if run.deleted_at is None:
                    run.deleted_at, run.deleted_by = now, access.actor.user_id
                if include_outputs:
                    for document in documents:
                        if document.id not in protected and document.deleted_at is None:
                            document.deleted_at, document.deleted_by = now, access.actor.user_id
                            document.deleted_by_run_id = run.id
            elif action == "PURGE":
                from skillmind.runs.history_purge import purge_run

                try:
                    references = await purge_run(
                        session,
                        run,
                        documents,
                        protected,
                        DocumentCleanupActor(
                            organization_id=access.actor.organization_id,
                            actor_id=access.actor.user_id,
                            request_id=access.request_id,
                            session_id=users.current_session.id,
                        ),
                        include_outputs,
                        storage,
                    )
                except IntegrityError as error:
                    authorize()
                    raise RunHistoryConflict(
                        "Other retained records reference this execution"
                    ) from error
            elif action == "RESTORE":
                for document in documents:
                    if document.deleted_by_run_id == run.id:
                        await require_restore_path(session, document)
                        document.deleted_at = document.deleted_by = document.deleted_by_run_id = (
                            None
                        )
                run.deleted_at = run.deleted_by = None
        except (RunHistoryConflict, DocumentConflictError, DocumentStorageUnavailableError):
            authorize()
            projects.require_active_write_access(project)
            raise
        await session.flush()
        authorize()
        projects.require_active_write_access(project)
        result: dict[str, Any] = {
            **preview,
            "deleted": action == "PURGE" or run.deleted_at is not None,
            "cleanup_pending": 0,
        }
    # commit 確認後のみ外部 byte を削除する。失敗は永続要求と応答へ残す。
    for reference in references:
        assert storage is not None
        try:
            await storage.delete(reference.key)
        except FileStorageError:
            result["cleanup_pending"] += 1
    return result


async def require_finished(session: AsyncSession, run: Run) -> None:
    """terminal でも稼働中 lease や不明操作が残れば隠さない。"""
    if RunStatus(run.status) not in TERMINAL_RUN_STATUSES:
        raise RunHistoryConflict("Only terminal runs can be deleted")
    active = await session.scalar(
        select(RunAttempt.id)
        .where(RunAttempt.run_id == run.id, RunAttempt.status.in_(["LEASED", "RUNNING"]))
        .limit(1)
    )
    effect = await session.scalar(
        select(EffectExecution.id)
        .where(
            EffectExecution.run_id == run.id,
            EffectExecution.status.in_(["REQUESTED", "LEASED", "APPLYING", "VERIFICATION_FAILED"]),
        )
        .limit(1)
    )
    pending = await session.scalar(
        select(McpDesktopLease.run_id).where(
            McpDesktopLease.run_id == run.id, McpDesktopLease.pending_effect_id.is_not(None)
        )
    )
    from skillmind.db.models import AgentSession, EffectReconciliationRequest, RunBudgetReservation

    agent = await session.scalar(
        select(AgentSession.id)
        .where(AgentSession.run_id == run.id, AgentSession.status == "ACTIVE")
        .limit(1)
    )
    reconciliation = await session.scalar(
        select(EffectReconciliationRequest.id)
        .where(
            EffectReconciliationRequest.run_id == run.id,
            EffectReconciliationRequest.status.in_(["QUEUED", "RUNNING"]),
        )
        .limit(1)
    )
    budget = await session.scalar(
        select(RunBudgetReservation.id)
        .where(
            RunBudgetReservation.run_id == run.id,
            RunBudgetReservation.status.in_(["RESERVED", "START_INTENT"]),
        )
        .limit(1)
    )
    if active or effect or pending or agent or reconciliation or budget:
        raise RunHistoryConflict("An operation still requires reconciliation")


async def require_restore_path(session: AsyncSession, document: ProjectDocument) -> None:
    """同名の新文書・待機 upload を上書きして復元しない。"""
    from skillmind.documents.management import path_conflicts
    from skillmind.documents.upload_repository import DocumentUploadRepository

    if await path_conflicts(
        session, document.project_id, document.folder, document.name, document.id
    ) or await DocumentUploadRepository(session).path_reserved(
        project_id=document.project_id, folder=document.folder, name=document.name
    ):
        raise DocumentConflictError("Restore destination exists")
