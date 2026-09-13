"""Run 開始時の固定同意を検証し、モデルや現在の既定値から承認権を作らない。"""

from __future__ import annotations

from uuid import UUID

from skillmind.db.models import ChangeApproval, Run
from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.domain import ApprovalSource, EffectLeaseValidationError
from skillmind.runs.creation_replay import stored_creation_intent
from skillmind.runs.creation_request import CREATION_REQUEST_FIELD
from skillmind.runs.domain import IdempotencyConflictError


def run_auto_approval_actor(run: Run, capability_version: str) -> UUID | None:
    """原要求の hash・開始者・登録済み能力を照合する。旧 Run は手動のままとする。"""

    if not resolve_effect_capability(capability_version).run_auto_approvable:
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
    return intent.actor_id if intent.auto_approve else None


def has_actor_approval(run: Run, approval: ChangeApproval, capability_version: str) -> bool:
    """段階実行と只読照会が同じ承認源を解釈し、Run 同意を他の開始者に流用しない。"""

    if approval.source == ApprovalSource.USER.value:
        return approval.actor_id is not None
    return (
        approval.source == ApprovalSource.RUN_START.value
        and approval.actor_id is not None
        and approval.preauthorization_id is None
        and approval.actor_id == run_auto_approval_actor(run, capability_version)
    )
