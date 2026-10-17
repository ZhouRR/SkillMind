"""同じ transaction 内で Proposal の原観測・カタログ・保存 byte を照合する。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.agent.postgres_native import query_identity
from skillmind.artifacts.repository import ArtifactRepository
from skillmind.db.models import (
    ChangeProposal,
    EffectExecution,
    Evidence,
    ResourceBinding,
    Run,
    ToolCall,
)
from skillmind.documents.library import DOCUMENT_WRITE_CAPABILITY
from skillmind.effects.database_write import DATABASE_WRITE_CAPABILITY, database_observation_matches
from skillmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from skillmind.effects.postgres_native import SQL_WRITE, SqlObservationValidationError


async def validate_mcp_observation(
    session: AsyncSession,
    draft: ChangeProposalDraft,
    *,
    binding: ResourceBinding,
    payload: dict[str, Any],
    allow_lookup: bool = False,
) -> str | None:
    """作成時だけ既存の発見証拠を補い、保存後は承認対象の参照だけを照合する。"""
    if draft.capability_version != "mcp.call/v1":
        return None
    if "cancel_target" in payload:
        original = (
            await session.scalars(
                select(EffectExecution)
                .join(ChangeProposal, ChangeProposal.id == EffectExecution.proposal_id)
                .where(
                    EffectExecution.id == UUID(payload["cancel_target"]),
                    EffectExecution.run_id == binding.run_id,
                    ChangeProposal.integration_id == binding.integration_id,
                    ChangeProposal.capability_version == "mcp.call/v1",
                    ChangeProposal.operation == "call",
                )
            )
        ).one_or_none()
        if original is None:
            raise ChangeProposalValidationError("MCP cancellation requires an original Run effect")
    statement = (
        select(Evidence.evidence_ref)
        .join(ToolCall, ToolCall.id == Evidence.tool_call_id)
        .where(
            Evidence.run_id == binding.run_id,
            ToolCall.run_id == binding.run_id,
            ToolCall.integration_id == binding.integration_id,
            ToolCall.provider == "mcp",
            ToolCall.capability_version == "mcp.tools/v1",
            ToolCall.status == "SUCCEEDED",
            Evidence.metadata_json["binding_checksum"].as_string() == binding.checksum,
            Evidence.source_locator["catalog_hash"].as_string() == payload["catalog_hash"],
        )
    )
    if not allow_lookup:
        statement = statement.where(Evidence.evidence_ref.in_(draft.evidence_refs))
    # 指定済みの証拠を優先し、同じ契約の再発見があっても一件だけを安定して選ぶ。
    evidence_ref = (
        await session.scalars(
            statement.order_by(
                Evidence.evidence_ref.in_(draft.evidence_refs).desc(),
                Evidence.created_at,
                Evidence.id,
            ).limit(1)
        )
    ).first()
    if evidence_ref is None:
        raise ChangeProposalValidationError("MCP call requires original catalog Evidence")
    return evidence_ref


async def validate_database_observation(
    session: AsyncSession,
    draft: ChangeProposalDraft,
    *,
    binding: ResourceBinding,
    payload: dict[str, Any],
) -> None:
    """DB 提案は同じ Run/binding の成功した read Tool 証拠に束縛し、自己申告を拒否する。"""

    if draft.capability_version == SQL_WRITE:
        await validate_native_sql_observation(session, draft, binding=binding, payload=payload)
        return
    if draft.capability_version != DATABASE_WRITE_CAPABILITY:
        return
    evidence = (
        await session.scalars(
            select(Evidence)
            .join(ToolCall, ToolCall.id == Evidence.tool_call_id)
            .where(
                Evidence.run_id == binding.run_id,
                Evidence.evidence_ref.in_(draft.evidence_refs),
                Evidence.evidence_type == "database",
                ToolCall.run_id == binding.run_id,
                ToolCall.integration_id == binding.integration_id,
                ToolCall.provider == "postgres",
                ToolCall.capability_version.in_(["database.read/v1", "database.read/v2"]),
                ToolCall.status == "SUCCEEDED",
            )
        )
    ).all()
    if binding.integration_id is None or not any(
        item.metadata_json.get("binding_checksum") == binding.checksum
        and database_observation_matches(
            item.source_locator, payload, integration_id=binding.integration_id
        )
        for item in evidence
    ):
        raise ChangeProposalValidationError("Database proposal requires exact read Evidence")


async def validate_native_sql_observation(
    session: AsyncSession,
    draft: ChangeProposalDraft,
    *,
    binding: ResourceBinding,
    payload: dict[str, Any],
) -> None:
    """同一 Run/binding の完全な SELECT 観測だけを DML の前提として受け入れる。"""
    identity = query_identity(payload["read_back"], binding.scope_json)
    evidence = (
        await session.scalars(
            select(Evidence)
            .join(ToolCall, ToolCall.id == Evidence.tool_call_id)
            .where(
                Evidence.run_id == binding.run_id,
                Evidence.evidence_ref.in_(draft.evidence_refs),
                Evidence.evidence_type == "resource",
                Evidence.content_hash == draft.precondition["revision"],
                ToolCall.run_id == binding.run_id,
                ToolCall.integration_id == binding.integration_id,
                ToolCall.provider == "postgres",
                ToolCall.capability_version == "database.query/v1",
                ToolCall.status == "SUCCEEDED",
            )
        )
    ).all()
    if binding.integration_id is None or not any(
        item.source_locator.get("query_identity") == identity
        and item.metadata_json.get("binding_checksum") == binding.checksum
        and item.metadata_json.get("truncated") is False
        for item in evidence
    ):
        raise SqlObservationValidationError("Native SQL requires matching query Evidence")


async def validate_effect_artifact(
    session: AsyncSession, *, run: Run, draft: ChangeProposalDraft, payload: dict[str, Any]
) -> None:
    """文書保存では原 Run の検証済み byte を必須とし、提案の自己申告だけで批准しない。"""

    if draft.capability_version != DOCUMENT_WRITE_CAPABILITY:
        return
    if draft.operation not in {"CREATE", "UPDATE"}:
        return
    try:
        artifact = await ArtifactRepository(session).get_content(
            project_id=run.project_id, run_id=run.id, artifact_ref=payload["artifact_ref"]
        )
        if artifact is None or any(
            getattr(artifact.metadata, name) != payload[key]
            for name, key in (
                ("checksum", "content_hash"),
                ("size_bytes", "size_bytes"),
            )
        ):
            raise ValueError("Original Artifact does not match the proposed content")
        # Artifact は現行 producer の text/plain。保存 MIME は批准した配信形式であり、
        # Markdown/JSON へのラベル指定で元 byte を変換・置換しない。
    except ValueError as error:
        raise ChangeProposalValidationError(
            "Document Artifact is unavailable or changed"
        ) from error
