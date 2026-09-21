"""Worker の永続操作を直接実行させない platform control Provider。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.proposal import CHANGE_PROPOSE_CAPABILITY


class UnavailableDocumentReadiness:
    """正本照会が未装配なら、前置条件を ready と推測しない。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """接続不足を安定した unavailable として扱う。"""
        raise ToolProviderError("unavailable", "Document readiness is unavailable", retryable=False)


class DeferredInteractionProvider:
    """PreToolUse defer が破られた場合に fail closed する control Provider。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """Interaction は Worker transaction だけが保存できるため直接実行を拒否する。"""

        del context, arguments
        raise ToolProviderError(
            "unavailable",
            "Interaction request must be deferred by Skillmind",
            retryable=False,
        )


class DeferredChangeProposalProvider:
    """新提案は拒否し、処理済みの原 control 呼出しだけを読取完了する。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """原要求へ最新 Brief を返す。Proposal 作成や外部 Provider 呼出しはしない。"""

        run = context.run
        resolved = run.resolved_proposal if run is not None else None
        if (
            run is not None and resolved is not None
            and context.run_id == run.run_id
            and context.run_attempt_id == run.run_attempt_id
            and context.project_id == run.project_id
            and context.user_id == run.user_id
            and context.tool.capability == CHANGE_PROPOSE_CAPABILITY
            and resolved.matches(arguments, resolved.sdk_session_id)
        ):
            # CLI は deferred replay 後、queued user prompt より先に自動で再開する。
            # その最初の model turn に確定回执と現 Brief を届け、古い段階の指示を使わせない。
            response = {
                "status": "success", "deferred": False,
                "proposal_ref": resolved.proposal_ref, "outcome": resolved.outcome,
                "continuation_prompt": run.prompt,
                "task_brief_checksum": run.task_brief_checksum,
            }
            return ProviderToolResult(
                response=response,
                evidence=(EvidenceDraft(
                    evidence_type="proposal_continuation",
                    source_uri=f"run://{run.run_id}/proposals/{resolved.proposal_ref}",
                    source_locator={"proposal_ref": resolved.proposal_ref},
                    content_hash="sha256:" + sha256_hex(canonical_json(response)),
                    metadata={"outcome": resolved.outcome,
                              "task_brief_checksum": run.task_brief_checksum},
                    excerpt="Read the original proposal outcome; no change was applied here.",
                ),),
            )
        raise ToolProviderError(
            "unavailable",
            "ChangeProposal request must be deferred by Skillmind",
            retryable=False,
        )
