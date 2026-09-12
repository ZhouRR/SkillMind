"""核対要求の原 identity、非公開 Worker owner と保存観測の共通検証。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.database_write import DatabaseWriteCommand
from skillmind.effects.postgres_write import DatabaseWriteReceipt, validate_database_write_receipt
from skillmind.effects.reconciliation_domain import (
    EffectReconciliationObservation,
    EffectReconciliationReference,
    EffectReconciliationTarget,
)
from skillmind.storage.effect_write import ObjectWriteReceipt

RECONCILIATION_DISPATCH_TOPIC = "effect.reconcile.dispatch/v1"
RECONCILIATION_QUEUE_TIMEOUT_SECONDS = 1800


class ReconciliationRequestNotFoundError(LookupError):
    """原要求が存在しない、または現在 actor に公開できない。"""


class ReconciliationRequestConflictError(RuntimeError):
    """同じ ID の内容差替え、または進行中の別核対要求がある。"""


@dataclass(frozen=True, slots=True)
class ReconciliationRequestSnapshot:
    """非公開会話参照を含む内部 snapshot。API は明示 whitelist で投影する。"""

    request_id: UUID
    reference: EffectReconciliationReference = field(repr=False)
    target_checksum: str
    command_checksum: str
    kind: Literal["DATABASE_TRANSACTION", "DOCUMENT_OBJECT"]
    status: str
    created_at: datetime
    finished_at: datetime | None
    observation_status: str | None
    observed_at: datetime | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ReconciliationRequestOwner:
    """開始 commit を確認した一つの Worker だけが保持する原 token。"""

    request: ReconciliationRequestSnapshot
    token: str = field(repr=False)


def reconciliation_command_checksum(target: EffectReconciliationTarget) -> str:
    """元 database/object 要求が既に持つ同じ checksum を用いる。"""

    command = target.command
    return (
        command.checksum if isinstance(command, DatabaseWriteCommand) else command.request_checksum
    )


def reconciliation_kind(
    target: EffectReconciliationTarget,
) -> Literal["DATABASE_TRANSACTION", "DOCUMENT_OBJECT"]:
    """transaction 回执と object byte の確認を混ぜない。"""

    return (
        "DATABASE_TRANSACTION"
        if isinstance(target.command, DatabaseWriteCommand)
        else "DOCUMENT_OBJECT"
    )


def reconciliation_target_checksum(target: EffectReconciliationTarget) -> str:
    """接続先/Secret locator は公開せず、受理時の原対象と現在対象の一致に束縛する。"""

    return "sha256:" + sha256_hex(
        canonical_json(
            {
                "proposal_id": str(target.proposal_id),
                "binding_id": str(target.binding_id),
                "proposal_checksum": target.proposal_checksum,
                "provider": target.provider,
                "provider_version": target.provider_version,
                "command_checksum": reconciliation_command_checksum(target),
                "config_json": target.config_json,
                "secret_reference_id": str(target.secret_reference_id)
                if target.secret_reference_id
                else None,
            }
        )
    )


def reconciliation_receipt_json(
    observation: EffectReconciliationObservation,
    target: EffectReconciliationTarget,
) -> dict[str, Any] | None:
    """原要求に属する有界 receipt だけを保存し、未検出/競合に receipt を補造しない。"""

    if (
        observation.effect_execution_id != target.command.effect_id
        or observation.request_checksum != reconciliation_command_checksum(target)
        or observation.kind != reconciliation_kind(target)
    ):
        raise ValueError("Reconciliation observation target does not match")
    receipt = observation.receipt
    if observation.status in {"NOT_OBSERVED", "CONFLICT"}:
        if receipt is not None:
            raise ValueError("Unconfirmed observation cannot contain a receipt")
        return None
    if observation.status != "CONFIRMED":
        raise ValueError("Reconciliation observation status is invalid")
    command = target.command
    if isinstance(command, DatabaseWriteCommand):
        if not isinstance(receipt, DatabaseWriteReceipt):
            raise ValueError("Original database receipt is invalid")
        validate_database_write_receipt(command, receipt)
        result = asdict(receipt)
    else:
        if (
            not isinstance(receipt, ObjectWriteReceipt)
            or receipt.effect_id != command.effect_id
            or receipt.request_checksum != command.request_checksum
            or receipt.object_key != command.object_key
            or receipt.content_checksum != command.content_checksum
            or receipt.size != len(command.content)
            or receipt.content_type != command.content_type
        ):
            raise ValueError("Original document receipt is invalid")
        result = {**asdict(receipt), "effect_id": str(receipt.effect_id)}
    if len(canonical_json(result).encode("utf-8")) > 2_097_152:
        raise ValueError("Original receipt exceeds its size limit")
    return result
