"""文書の目录操作を原 Effect 回执と同じ transaction で確定する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import DocumentMutationReceipt
from skillmind.documents.domain import DocumentConflictError
from skillmind.documents.file_state import FOLDER_OPERATIONS, observe_file_state
from skillmind.documents.management import DocumentManagementRepository
from skillmind.effects.domain import (
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectProviderResult,
)


@dataclass(frozen=True, slots=True)
class DocumentManagementCommand:
    """再適用権を含まない原目录操作の照会対象。"""

    effect_id: UUID
    project_id: UUID
    run_id: UUID
    request_checksum: str


@dataclass(frozen=True, slots=True)
class DocumentManagementReceipt:
    """変更と原子保存された最小回执。現在の遠端状態の代用ではない。"""

    effect_id: UUID
    request_checksum: str
    result: dict[str, Any]


async def lookup_management_receipt(
    session: AsyncSession,
    command: DocumentManagementCommand,
) -> DocumentManagementReceipt | None:
    """原 ID と checksum の保存記録だけを照会し、変更は一切行わない。"""
    row = await session.scalar(
        select(DocumentMutationReceipt).where(
            DocumentMutationReceipt.effect_id == command.effect_id,
            DocumentMutationReceipt.project_id == command.project_id,
            DocumentMutationReceipt.run_id == command.run_id,
        )
    )
    if row is None:
        return None
    if row.request_checksum != command.request_checksum:
        raise ValueError("Original document management receipt differs")
    return DocumentManagementReceipt(command.effect_id, row.request_checksum, row.result_json)


def mutation_checksum(
    execution: ClaimedEffectExecution | SimpleNamespace,
    payload: Mapping[str, Any],
) -> str:
    """原 Effect と精確な操作を束ね、別の対象への回执転用を拒否する。"""
    return "sha256:" + sha256_hex(
        canonical_json(
            {
                "effect_id": str(execution.effect_execution_id),
                "project_id": str(execution.project_id),
                "run_id": str(execution.run_id),
                "operation": execution.operation,
                "payload": payload,
            }
        )
    )


async def apply_document_management(
    session: AsyncSession,
    execution: ClaimedEffectExecution,
    payload: Mapping[str, Any],
    *,
    actor_id: UUID,
) -> EffectProviderResult:
    """Org/Project/Effect 認可 lock 内で現在版を確認し、未確定操作を再送しない。"""
    checksum = mutation_checksum(execution, payload)
    receipt = await session.scalar(
        select(DocumentMutationReceipt)
        .where(
            DocumentMutationReceipt.effect_id == execution.effect_execution_id,
        )
        .with_for_update()
    )
    if receipt is not None:
        if (
            receipt.project_id != execution.project_id
            or receipt.run_id != execution.run_id
            or receipt.request_checksum != checksum
        ):
            raise ValueError("Document management receipt identity differs")
        return management_result(execution, receipt.result_json, checksum, replayed=True)
    operation, path = execution.operation, payload["path"]
    before = await observe_file_state(
        session,
        project_id=execution.project_id,
        path=path,
        folder=operation in FOLDER_OPERATIONS,
        trashed=operation == "RESTORE",
        document_id=payload.get("document_id"),
    )
    if operation == "CREATE_FOLDER":
        if before["exists"]:
            raise DocumentConflictError("Directory already exists")
    elif before["revision"] != payload["expected_revision"]:
        raise DocumentConflictError(
            "Document changed; observe the current version before proposing again"
        )
    changes: list[dict[str, Any]] = []
    if operation not in FOLDER_OPERATIONS:
        document = before["document"]
        if document is None:
            raise DocumentConflictError("Original document is unavailable")
        folder, _, name = path.rpartition("/")
        changes = [
            {
                "document_id": UUID(document["document_id"]),
                "expected_folder": folder,
                "expected_name": name,
            }
        ]
        if operation == "MOVE":
            folder, _, name = payload["destination"].rpartition("/")
            changes[0].update(folder=folder, name=name)
    await DocumentManagementRepository(session).apply(
        execution.project_id,
        operation,
        changes,
        path if operation in {"MOVE_FOLDER", "DELETE_FOLDER"} else None,
        path if operation == "CREATE_FOLDER" else payload.get("destination"),
        actor_id=actor_id,
    )
    await session.flush()
    after = await observe_file_state(
        session,
        project_id=execution.project_id,
        path=payload.get("destination", path),
        folder=operation in FOLDER_OPERATIONS,
        trashed=operation == "TRASH",
        document_id=before.get("document", {}).get("document_id")
        if before.get("document")
        else None,
    )
    saved = {"before": before, "after": after, "operation": operation}
    session.add(
        DocumentMutationReceipt(
            id=uuid4(),
            effect_id=execution.effect_execution_id,
            run_id=execution.run_id,
            project_id=execution.project_id,
            request_checksum=checksum,
            result_json=saved,
            created_at=datetime.now(UTC),
        )
    )
    return management_result(execution, saved, checksum, replayed=False)


def management_result(
    execution: ClaimedEffectExecution,
    saved: Mapping[str, Any],
    checksum: str,
    *,
    replayed: bool,
) -> EffectProviderResult:
    """共有 Effect の before/after と回读形式へ変換し、画面の文書正本へ接続する。"""

    def evidence(phase: str) -> EffectEvidenceDraft:
        """前後とも実測 metadata だけを保持し、本文や資格情報を含めない。"""
        return EffectEvidenceDraft(
            evidence_type="document",
            source_uri=f"project-document://{execution.project_id}/management",
            source_locator={"effect_id": str(execution.effect_execution_id), "phase": phase},
            content={"document": saved[phase]},
            excerpt=None,
            metadata={"provider": "project-library", "request_checksum": checksum},
        )

    return EffectProviderResult(
        before=evidence("before"),
        after=evidence("after"),
        verification={
            "method": "READ_BACK",
            "matched_paths": ["/document"],
            "effect_id": str(execution.effect_execution_id),
            "request_checksum": checksum,
            "operation": saved["operation"],
            "replayed": replayed,
        },
        replayed=replayed,
    )
