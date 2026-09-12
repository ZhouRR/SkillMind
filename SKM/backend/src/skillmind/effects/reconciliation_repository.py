"""核対要求の同一性、単一 owner と独立観測を caller-owned transaction で保存する。"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.core.hashing import canonical_json
from skillmind.db.models import EffectExecution, EffectReconciliationRequest, OutboxMessage, Run
from skillmind.effects.reconciliation_domain import (
    EffectReconciliationDeniedError,
    EffectReconciliationObservation,
    EffectReconciliationReference,
    EffectReconciliationTarget,
)
from skillmind.effects.reconciliation_requests import (
    RECONCILIATION_DISPATCH_TOPIC,
    RECONCILIATION_QUEUE_TIMEOUT_SECONDS,
    ReconciliationRequestConflictError,
    ReconciliationRequestNotFoundError,
    ReconciliationRequestOwner,
    ReconciliationRequestSnapshot,
    reconciliation_command_checksum,
    reconciliation_kind,
    reconciliation_receipt_json,
    reconciliation_target_checksum,
)
from skillmind.runs.domain import lease_token_hash


class ReconciliationRequestRepository:
    """Org/現在会話/Project/Run の認可後に request lock を取り、外部 I/O は行わない。"""

    def __init__(self, session: AsyncSession) -> None:
        """commit と認可は service が所有する。"""
        self.session = session

    async def find(
        self, request_id: UUID, *, lock: bool = False
    ) -> EffectReconciliationRequest | None:
        """元 request のみを読み、lock 時には古い ORM cache を利用しない。"""
        query = select(EffectReconciliationRequest).where(
            EffectReconciliationRequest.id == request_id
        )
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        row: EffectReconciliationRequest | None = await self.session.scalar(query)
        return row

    async def require(self, request_id: UUID, *, lock: bool = False) -> EffectReconciliationRequest:
        """未知 Queue ID へ新しい会話や target を補造しない。"""
        row = await self.find(request_id, lock=lock)
        if row is None:
            raise ReconciliationRequestNotFoundError("Reconciliation request was not found")
        return row

    async def latest(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        effect_execution_id: UUID,
    ) -> EffectReconciliationRequest | None:
        """原 Effect の帰属を確認し、履歴全件を装わず最新要求だけを取得する。"""
        found = await self.session.scalar(
            select(EffectExecution.id)
            .join(Run, Run.id == EffectExecution.run_id)
            .where(
                Run.id == run_id,
                Run.project_id == project_id,
                EffectExecution.id == effect_execution_id,
            )
        )
        if found is None:
            raise ReconciliationRequestNotFoundError("Original effect was not found")
        row: EffectReconciliationRequest | None = await self.session.scalar(
            select(EffectReconciliationRequest)
            .where(
                EffectReconciliationRequest.project_id == project_id,
                EffectReconciliationRequest.run_id == run_id,
                EffectReconciliationRequest.effect_execution_id == effect_execution_id,
            )
            .order_by(
                EffectReconciliationRequest.created_at.desc(), EffectReconciliationRequest.id.desc()
            )
            .limit(1)
        )
        return row

    async def expired(self, *, now: datetime, limit: int) -> tuple[UUID, ...]:
        """回収候補だけを列挙し、実変更時は原会話と request lock の下で再判定する。"""
        rows = await self.session.scalars(
            select(EffectReconciliationRequest.id)
            .where(
                or_(
                    (EffectReconciliationRequest.status == "RUNNING")
                    & (EffectReconciliationRequest.lease_expires_at <= now),
                    (EffectReconciliationRequest.status == "QUEUED")
                    & (
                        EffectReconciliationRequest.created_at
                        <= now
                        - timedelta(
                            seconds=RECONCILIATION_QUEUE_TIMEOUT_SECONDS,
                        )
                    ),
                ),
            )
            .order_by(EffectReconciliationRequest.lease_expires_at, EffectReconciliationRequest.id)
            .limit(limit)
        )
        return tuple(rows)

    async def create(
        self,
        *,
        request_id: UUID,
        reference: EffectReconciliationReference,
        accepted_http_request_id: UUID,
        target: EffectReconciliationTarget,
        now: datetime,
    ) -> EffectReconciliationRequest:
        """現在 Org gate の下で一つの active request と ID だけの Outbox を同時追加する。"""
        if any(
            not isinstance(value, UUID) or value.int == 0
            for value in (
                request_id,
                accepted_http_request_id,
                reference.organization_id,
                reference.actor_id,
                reference.auth_session_id,
                reference.project_id,
                reference.run_id,
                reference.effect_execution_id,
            )
        ):
            raise ValueError("Reconciliation request identity is invalid")
        if (
            reference.project_id != target.command.project_id
            or reference.run_id != target.command.run_id
            or reference.effect_execution_id != target.command.effect_id
        ):
            raise ValueError("Reconciliation reference does not match its target")
        existing = await self.session.scalar(
            select(EffectReconciliationRequest)
            .where(
                EffectReconciliationRequest.effect_execution_id == reference.effect_execution_id,
                EffectReconciliationRequest.status.in_(("QUEUED", "RUNNING")),
            )
            .with_for_update()
        )
        if existing is not None:
            raise ReconciliationRequestConflictError("Original effect has an active lookup")
        row = EffectReconciliationRequest(
            id=request_id,
            organization_id=reference.organization_id,
            actor_id=reference.actor_id,
            auth_session_id=reference.auth_session_id,
            accepted_http_request_id=accepted_http_request_id,
            project_id=reference.project_id,
            run_id=reference.run_id,
            effect_execution_id=reference.effect_execution_id,
            target_checksum=reconciliation_target_checksum(target),
            command_checksum=reconciliation_command_checksum(target),
            kind=reconciliation_kind(target),
            status="QUEUED",
            owner_hash=None,
            claimed_at=None,
            lease_expires_at=None,
            created_at=now,
            finished_at=None,
            observation_status=None,
            observed_at=None,
            receipt_json=None,
            error_code=None,
        )
        self.session.add(row)
        await self.session.flush()
        self.session.add(
            OutboxMessage(
                id=uuid4(),
                aggregate_type="effect_reconciliation_request",
                aggregate_id=request_id,
                topic=RECONCILIATION_DISPATCH_TOPIC,
                payload_json={"request_id": str(request_id)},
                occurred_at=now,
                published_at=None,
                publish_attempts=0,
                error_json=None,
            )
        )
        return row

    @staticmethod
    def reference(row: EffectReconciliationRequest) -> EffectReconciliationReference:
        """Worker は受理行に保存した actor/会話/target だけを使用する。"""
        return EffectReconciliationReference(
            row.organization_id,
            row.actor_id,
            row.auth_session_id,
            row.project_id,
            row.run_id,
            row.effect_execution_id,
        )

    @staticmethod
    def snapshot(row: EffectReconciliationRequest) -> ReconciliationRequestSnapshot:
        """秘密・owner hash・内部 receipt を除外し、公開用明示投影の入力を返す。"""
        if row.kind not in {"DATABASE_TRANSACTION", "DOCUMENT_OBJECT"}:
            raise ValueError("Invalid reconciliation kind")
        return ReconciliationRequestSnapshot(
            row.id,
            ReconciliationRequestRepository.reference(row),
            row.target_checksum,
            row.command_checksum,
            "DATABASE_TRANSACTION" if row.kind == "DATABASE_TRANSACTION" else "DOCUMENT_OBJECT",
            row.status,
            row.created_at,
            row.finished_at,
            row.observation_status,
            row.observed_at,
            row.error_code,
        )

    @staticmethod
    def claim(
        row: EffectReconciliationRequest,
        *,
        token: str,
        now: datetime,
    ) -> ReconciliationRequestOwner | None:
        """QUEUED から一度だけ owner を作り、commit 前の返却値を実行許可として公開しない。"""
        if row.status != "QUEUED":
            return None
        if len(token) < 32:
            raise ValueError("Reconciliation owner token is invalid")
        row.status, row.owner_hash = "RUNNING", lease_token_hash(token)
        row.claimed_at, row.lease_expires_at = now, now + timedelta(seconds=60)
        return ReconciliationRequestOwner(ReconciliationRequestRepository.snapshot(row), token)

    @staticmethod
    def require_owner(
        row: EffectReconciliationRequest,
        owner: ReconciliationRequestOwner,
        *,
        now: datetime,
        completed: bool = False,
    ) -> None:
        """Queue 再配送や旧 Worker に元 token を再発行せず、現在 owner と完全参照を照合する。"""
        expected = owner.request
        if (
            row.id != expected.request_id
            or ReconciliationRequestRepository.reference(row) != expected.reference
            or row.target_checksum != expected.target_checksum
            or row.command_checksum != expected.command_checksum
            or row.kind != expected.kind
            or row.created_at != expected.created_at
            or row.owner_hash is None
            or not hmac.compare_digest(row.owner_hash, lease_token_hash(owner.token))
            or (
                not completed
                and (
                    row.status != "RUNNING"
                    or row.lease_expires_at is None
                    or row.lease_expires_at <= now
                )
            )
        ):
            raise EffectReconciliationDeniedError("Reconciliation owner is no longer valid")

    @staticmethod
    def finish(
        row: EffectReconciliationRequest,
        owner: ReconciliationRequestOwner,
        *,
        observation: EffectReconciliationObservation,
        target: EffectReconciliationTarget,
        now: datetime,
    ) -> None:
        """原観測を一度だけ保存し、Effect/Run の状態や配額は変更しない。"""
        replay = row.status == "SUCCEEDED"
        ReconciliationRequestRepository.require_owner(row, owner, now=now, completed=replay)
        if reconciliation_target_checksum(target) != row.target_checksum:
            raise ReconciliationRequestConflictError("Accepted reconciliation target changed")
        receipt = reconciliation_receipt_json(observation, target)
        if replay:
            if (
                row.observation_status != observation.status
                or row.observed_at != observation.observed_at
                or canonical_json(row.receipt_json) != canonical_json(receipt)
            ):
                raise ReconciliationRequestConflictError("Original reconciliation result changed")
            return
        if (
            row.claimed_at is None
            or observation.observed_at < row.claimed_at
            or observation.observed_at > now
        ):
            raise ValueError("Reconciliation observation time is invalid")
        row.status, row.finished_at = "SUCCEEDED", now
        row.observation_status, row.observed_at = observation.status, observation.observed_at
        row.receipt_json, row.error_code = receipt, None

    @staticmethod
    def fail(row: EffectReconciliationRequest, *, code: str, now: datetime) -> bool:
        """読取停止を記録して active 枠を閉じ、元書込の未実行を主張しない。"""
        if code not in {
            "lookup_unavailable",
            "lookup_interrupted",
            "authorization_revoked",
            "target_changed",
        }:
            raise ValueError("Invalid reconciliation failure code")
        if row.status not in {"QUEUED", "RUNNING"}:
            return False
        row.status = "REVOKED" if code == "authorization_revoked" else "FAILED"
        row.finished_at, row.error_code = now, code
        return True
