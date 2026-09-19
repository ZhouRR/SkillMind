"""終了済み回収箱の明示完全削除。外部業務 DB や操作を再実行しない。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db import models as m
from skillmind.documents.content import require_document_storage
from skillmind.documents.domain import DocumentCleanupActor
from skillmind.documents.repository import DocumentRepository
from skillmind.runs.history_deletion import RunHistoryConflict, require_finished
from skillmind.storage import BlobReference, FileStorage


async def purge_run(
    session: AsyncSession,
    run: m.Run,
    documents: list[m.ProjectDocument],
    protected: list[UUID],
    actor: DocumentCleanupActor,
    include_outputs: bool,
    storage: FileStorage | None,
) -> list[BlobReference]:
    """原 Run に属する行だけを FK 順に除き、独立した削除監査と blob 清理要求を残す。"""
    if run.deleted_at is None:
        raise RunHistoryConflict("Move the run to the recycle bin first")
    await require_finished(session, run)
    # 未公開 upload の結果は不明。受付・予約を破棄して外部書込の根拠を失わない。
    pending = await session.scalar(
        select(m.ProjectDocumentEffectUpload.id)
        .where(
            m.ProjectDocumentEffectUpload.run_id == run.id,
            m.ProjectDocumentEffectUpload.state != "PUBLISHED",
        )
        .limit(1)
    )
    if pending:
        raise RunHistoryConflict("An unpublished output requires reconciliation")
    repository = DocumentRepository(session)
    references = []
    for document in documents:
        if include_outputs and document.id not in protected:
            _, reference = await repository.get_for_download(
                project_id=run.project_id, document_id=document.id
            )
            if storage is None:
                raise RunHistoryConflict("Document storage is unavailable")
            require_document_storage(storage, reference)
            references.append(
                await repository.delete(
                    project_id=run.project_id,
                    document_id=document.id,
                    cleanup_actor=actor,
                    allow_effect=True,
                )
            )
        else:
            # 残す文書の内容と原保存先は変更せず、削除対象への FK だけ解除する。
            document.effect_upload_id = None
            document.deleted_by_run_id = None
    await session.flush()
    await session.execute(
        update(m.ProjectDocument)
        .where(m.ProjectDocument.deleted_by_run_id == run.id)
        .values(deleted_by_run_id=None)
    )
    # Schedule 設定は残す。削除対象に付随する occurrence のみ除く。
    await session.execute(
        update(m.TaskSchedule).where(m.TaskSchedule.last_run_id == run.id).values(last_run_id=None)
    )
    await session.execute(
        delete(m.TaskScheduleOccurrence).where(m.TaskScheduleOccurrence.run_id == run.id)
    )
    for model, kind in (
        (m.EffectExecution, "effect_execution"),
        (m.EffectReconciliationRequest, "effect_reconciliation_request"),
    ):
        await session.execute(
            delete(m.OutboxMessage).where(
                m.OutboxMessage.aggregate_type == kind,
                m.OutboxMessage.aggregate_id.in_(select(model.id).where(model.run_id == run.id)),
            )
        )
    await session.execute(
        delete(m.OutboxMessage).where(
            m.OutboxMessage.aggregate_type == "run", m.OutboxMessage.aggregate_id == run.id
        )
    )
    await session.execute(
        delete(m.Evaluation).where(
            m.Evaluation.result_id.in_(select(m.RunResult.id).where(m.RunResult.run_id == run.id))
        )
    )
    reservations = select(m.RunBudgetReservation.id).where(m.RunBudgetReservation.run_id == run.id)
    for budget_model in (m.RunBudgetObservation, m.RunBudgetReceipt):
        await session.execute(
            delete(budget_model).where(budget_model.reservation_id.in_(reservations))
        )
    # Segment の循環参照だけを切る。外部 Run の FK は解除せず、全体 rollback にする。
    await session.execute(
        update(m.RunSegment)
        .where(m.RunSegment.run_id == run.id)
        .values(parent_agent_session_id=None, instruction_snapshot_id=None)
    )
    for owned_model in (
        m.McpDesktopLease,
        m.ProjectDocumentEffectUpload,
        m.EffectReconciliationRequest,
        m.InteractionResponse,
        m.UserInteraction,
        m.EffectExecution,
        m.ChangeApproval,
        m.ChangeProposal,
        m.Evidence,
        m.PermissionDecision,
        m.ToolCall,
        m.RunResult,
        m.RunEvent,
        m.RunInputSnapshot,
        m.RunBudgetReservation,
        m.RunBudgetAccount,
        m.AgentTaskBriefSnapshot,
        m.AgentSession,
        m.ResourceBinding,
        m.RunAttempt,
        m.RunSegment,
        m.RunSkillSnapshot,
    ):
        await session.execute(delete(owned_model).where(owned_model.run_id == run.id))
    session.add(
        m.RunDeletionAudit(
            id=uuid4(),
            project_id=run.project_id,
            run_id=run.id,
            actor_id=actor.actor_id,
            request_id=actor.request_id,
            task_id=run.task_id,
            idempotency_key=run.idempotency_key,
            output_count=len(references),
            created_at=datetime.now(UTC),
        )
    )
    await session.execute(delete(m.Run).where(m.Run.id == run.id))
    return references
