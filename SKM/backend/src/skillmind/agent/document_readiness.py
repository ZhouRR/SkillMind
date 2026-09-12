"""文書前置条件の正本を、モデルに接続や外部書込権を渡さず公開する。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import Run
from skillmind.runs.document_prerequisites import load_document_readiness
from skillmind.skills.document_prerequisites import DOCUMENT_READINESS_CAPABILITY


class DocumentReadinessProvider:
    """現在 Run のみを照会し、checkpoint の自己申告から ready を導出しない。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Tool audit と同じ application DB の短い読取 session を使う。"""
        self._session_factory = session_factory

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """未達条件も成功した読取結果として返し、文書取得の許可とは区別する。"""
        if (
            context.run is None
            or context.run.run_id != context.run_id
            or context.run.project_id != context.project_id
            or context.run.run_attempt_id != context.run_attempt_id
            or context.run.user_id != context.user_id
            or context.tool.capability != DOCUMENT_READINESS_CAPABILITY
            or DOCUMENT_READINESS_CAPABILITY
            not in context.run.permission_snapshot.get("allowed_capabilities", [])
        ):
            raise ToolProviderError(
                "scope_denied", "Readiness inspection is not allowed", retryable=False
            )
        try:
            async with asyncio.timeout(20), self._session_factory() as session:
                run = (
                    await session.scalars(
                        select(Run).where(
                            Run.id == context.run_id,
                            Run.project_id == context.project_id,
                        )
                    )
                ).one_or_none()
                if run is None:
                    raise ValueError("Original Run is unavailable")
                state = await load_document_readiness(session, run)
        except (SQLAlchemyError, ValueError, TypeError, TimeoutError) as error:
            raise ToolProviderError(
                "unavailable", "Document prerequisites could not be verified", retryable=False
            ) from error
        response = {
            "status": "success",
            "provider": "platform",
            "ready": state.ready,
            "required_effect_intents": list(state.required),
            "applied_effect_intents": list(state.applied),
        }
        return ProviderToolResult(
            response=response,
            evidence=(
                EvidenceDraft(
                    evidence_type="document_readiness",
                    source_uri=f"run://{context.run_id}/document-readiness",
                    source_locator={"run_id": str(context.run_id)},
                    content_hash="sha256:" + sha256_hex(canonical_json(response)),
                    metadata=response,
                    excerpt=(
                        "Confirmed original-Run effects; this read does not approve "
                        "or apply a change."
                    ),
                ),
            ),
        )
