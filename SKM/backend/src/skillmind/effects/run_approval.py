"""Run 開始時の固定同意を検証し、モデルや現在の既定値から承認権を作らない。"""

from __future__ import annotations

from uuid import UUID

from skillmind.db.models import ChangeApproval, Run
from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.domain import ApprovalSource, EffectLeaseValidationError
from skillmind.runs.creation_replay import stored_creation_intent
from skillmind.runs.creation_request import CREATION_REQUEST_FIELD
from skillmind.runs.domain import IdempotencyConflictError


def run_auto_approval_actor(
    run: Run, capability_version: str, *, provider: str | None = None
) -> UUID | None:
    """原要求の hash・開始者・登録済み能力を照合する。旧 Run は手動のままとする。"""

    if not resolve_effect_capability(capability_version).supports_run_approval(provider):
        return None
    value = run.task_snapshot_json.get(CREATION_REQUEST_FIELD)
    if not isinstance(value, dict) or value.get("auto_approve") is not True:
        return None
    try:
        intent = stored_creation_intent(run)
    except IdempotencyConflictError as error:
        raise EffectLeaseValidationError(
            "Run start approval integrity could not be verified"
        ) from error
    # v2 の同意は DB/文書だけ。新しい既定値で旧 Run の Git 権限を増やさない。
    if capability_version == "repository.write/v1" and not intent.auto_approve_git:
        return None
    if capability_version == "mcp.call/v1" and not intent.auto_approve_mcp:
        return None
    return intent.actor_id if intent.auto_approve else None


def has_actor_approval(
    run: Run, approval: ChangeApproval, capability_version: str, *, provider: str | None = None
) -> bool:
    """段階実行と只読照会が同じ承認源を解釈し、Run 同意を他の開始者に流用しない。"""

    if approval.source == ApprovalSource.USER.value:
        return approval.actor_id is not None
    return (
        approval.source == ApprovalSource.RUN_START.value
        and approval.actor_id is not None
        and approval.preauthorization_id is None
        and approval.actor_id == run_auto_approval_actor(run, capability_version, provider=provider)
    )
