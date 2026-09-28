"""明示確認済みの帰档 Project を一括削除し、監査と blob 清理対象を独立して残す。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db import models as m
from skillmind.documents.content import require_document_storage
from skillmind.documents.domain import (
    DocumentCleanupActor,
    DocumentStorageUnavailableError,
    DocumentUploadError,
)
from skillmind.documents.repository import DocumentRepository
from skillmind.projects.domain import (
    ProjectDeleteBlockedError,
    ProjectStatus,
    require_project_version,
)
from skillmind.projects.repository import _PROJECT_OWNED_MODELS
from skillmind.runs.history_deletion import RunHistoryConflict, require_finished
from skillmind.runs.history_purge import purge_run
from skillmind.storage import BlobReference, FileStorage


async def purge_project(
    session: AsyncSession,
    *,
    project: m.Project,
    expected_row_version: int,
    actor: DocumentCleanupActor,
    storage: FileStorage | None,
) -> list[BlobReference]:
    """Org→User→Session→Project lock 内で事前検査し、外部 DELETE は commit 後へ渡す。"""
    require_project_version(project.row_version, expected_row_version)
    if project.status != ProjectStatus.ARCHIVED.value:
        raise ProjectDeleteBlockedError(
            "Archive the project first", blockers=("project_not_archived",)
        )
    project_id = project.id
    runs = list(
        await session.scalars(
            select(m.Run)
            .where(m.Run.project_id == project_id)
            .order_by(m.Run.created_at.desc(), m.Run.id)
            .with_for_update()
        )
    )
    try:
        for run in runs:
            await require_finished(session, run)
    except RunHistoryConflict as error:
        raise ProjectDeleteBlockedError(
            "An execution is active or requires reconciliation",
            blockers=("run_history_exists",),
        ) from error
    if await session.scalar(
        select(m.TaskScheduleOccurrence.id)
        .where(
            m.TaskScheduleOccurrence.project_id == project_id,
            m.TaskScheduleOccurrence.status == "PENDING",
        )
        .limit(1)
    ):
        raise ProjectDeleteBlockedError(
            "A schedule occurrence requires reconciliation",
            blockers=("task_schedule_exists",),
        )
    for model in (m.ProjectDocumentUpload, m.ProjectDocumentEffectUpload):
        if await session.scalar(
            select(model.id)
            .where(
                model.project_id == project_id,
                model.state != "PUBLISHED",
            )
            .limit(1)
        ):
            raise ProjectDeleteBlockedError(
                "A document upload requires reconciliation",
                blockers=("document_upload_exists",),
            )
    documents = list(
        await session.scalars(
            select(m.ProjectDocument)
            .where(m.ProjectDocument.project_id == project_id)
            .order_by(m.ProjectDocument.id)
            .with_for_update()
        )
    )
    repository = DocumentRepository(session)
    references: list[BlobReference] = []
    try:
        for document in documents:
            _, reference = await repository.get_for_download(
                project_id=project_id,
                document_id=document.id,
            )
            if storage is None:
                raise DocumentStorageUnavailableError("Document storage is unavailable")
            require_document_storage(storage, reference)
            # 壊れた旧 row が別 Project の同じ実体を指す場合、その byte は消さない。
            if await session.scalar(
                select(m.ProjectDocument.id)
                .where(
                    m.ProjectDocument.project_id != project_id,
                    m.ProjectDocument.storage_namespace_id == document.storage_namespace_id,
                    m.ProjectDocument.storage_key == document.storage_key,
                )
                .limit(1)
            ):
                raise DocumentStorageUnavailableError(
                    "Document storage is shared with another project"
                )
            references.append(reference)
        for document in documents:
            await repository.delete(
                project_id=project_id,
                document_id=document.id,
                cleanup_actor=actor,
                allow_effect=True,
            )
    except (DocumentStorageUnavailableError, DocumentUploadError) as error:
        raise ProjectDeleteBlockedError(
            "The original document storage or upload could not be verified",
            blockers=("document_upload_exists",),
        ) from error
    await session.flush()
    # 同 Project の調度と子実行だけを除く。組織 Skill や外部業務 DB は対象にしない。
    await session.execute(
        delete(m.TaskScheduleOccurrence).where(
            m.TaskScheduleOccurrence.project_id == project_id,
        )
    )
    await session.execute(delete(m.TaskSchedule).where(m.TaskSchedule.project_id == project_id))
    for run in runs:
        if run.deleted_at is None:
            run.deleted_at, run.deleted_by = datetime.now(UTC), actor.actor_id
        await purge_run(session, run, [], [], actor, False, None)
    await session.execute(
        update(m.User)
        .where(
            m.User.organization_id == project.organization_id,
            m.User.preferred_project_id == project_id,
        )
        .values(preferred_project_id=None)
    )
    for owned_model in _PROJECT_OWNED_MODELS:
        await session.execute(delete(owned_model).where(owned_model.project_id == project_id))
    session.add(
        m.ProjectDeletionAudit(
            organization_id=project.organization_id,
            project_id=project_id,
            project_key=project.key,
            actor_id=actor.actor_id,
            request_id=actor.request_id,
            run_count=len(runs),
            document_count=len(documents),
            created_at=datetime.now(UTC),
        )
    )
    await session.delete(project)
    return references
