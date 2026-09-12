"""書込権を含まない、原 Effect の照会参照と内部観測結果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from skillmind.effects.database_write import DatabaseWriteCommand
from skillmind.effects.postgres_write import DatabaseWriteReceipt
from skillmind.storage.effect_write import ObjectWriteCommand, ObjectWriteReceipt


class EffectReconciliationDeniedError(PermissionError):
    """原照会者の会話または現在の Project 参照権が失効している。"""


class EffectReconciliationUnavailableError(RuntimeError):
    """元 target/回执の観測を確認できず、未実行とは判定できない。"""


@dataclass(frozen=True, slots=True)
class EffectReconciliationReference:
    """認証済み要求と同じ TX で保存する参照。HTTP/Queue 本文から直接構築してはならない。"""

    organization_id: UUID
    actor_id: UUID
    auth_session_id: UUID
    project_id: UUID
    run_id: UUID
    effect_execution_id: UUID


@dataclass(frozen=True, slots=True)
class EffectReconciliationTarget:
    """元提案から再構築した要求。lease/token を持たず、claim/apply の入力にしない。"""

    proposal_id: UUID
    binding_id: UUID
    proposal_checksum: str
    provider: str
    provider_version: str
    command: DatabaseWriteCommand | ObjectWriteCommand = field(repr=False)
    config_json: str = field(repr=False)
    secret_reference_id: UUID | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class EffectReconciliationInput:
    """現在の読み取り資格付き内部入力。秘密は repr/公開観測へ含めない。"""

    target: EffectReconciliationTarget
    credential: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class EffectReconciliationObservation:
    """一回の読取事実。元 transaction と object の確認は、平台の APPLIED と区別する。"""

    effect_execution_id: UUID
    kind: Literal["DATABASE_TRANSACTION", "DOCUMENT_OBJECT"]
    status: Literal["CONFIRMED", "NOT_OBSERVED", "CONFLICT"]
    observed_at: datetime
    request_checksum: str
    receipt: DatabaseWriteReceipt | ObjectWriteReceipt | None = field(repr=False)
