"""Run 共通予算の短い DB transaction。外部起動や停止核対はこの境界内で行わない。"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import inspect, select

from projectmind.db.models import (
    Run,
    RunAttempt,
    RunBudgetAccount,
    RunBudgetReceipt,
    RunBudgetReservation,
    RunSegment,
)
from projectmind.runs.budget import (
    BudgetAccountRecord,
    BudgetConflictError,
    BudgetError,
    BudgetExecutionRecord,
    BudgetExecutionStatus,
    BudgetExhaustedError,
    BudgetPolicy,
    BudgetReceiptResult,
    BudgetReconciliationClaim,
    BudgetReservationRequest,
    BudgetUnavailableError,
    BudgetUsageReport,
    MeteringMode,
    budget_checksum,
    budget_key,
    usd_to_nanos,
)
from projectmind.runs.domain import ClaimedRun, LeaseValidationError, RunStatus, lease_token_hash
from projectmind.runs.repository_base import _RunRepositoryBase


def _stored_units(value: Decimal | int | None) -> int:
    """DB の整数値を丸めず読み、Numeric の NaN や部分単位も拒否する。"""

    if value is None or (isinstance(value, Decimal) and not value.is_finite()):
        raise BudgetUnavailableError("Stored budget amount is not finite")
    integer = int(value)
    if integer < 0 or value != integer:
        raise BudgetUnavailableError("Stored budget amount is not a nonnegative integer")
    return integer


def new_budget_account(run: Run, policy: BudgetPolicy) -> RunBudgetAccount:
    """新 Run の作成 transaction でだけ使う工場。保存済み Run をゼロ初期化しない。"""

    state = inspect(run)
    if (
        not (state.transient or state.pending)
        or run.status != RunStatus.QUEUED.value
        or run.started_at is not None
        or policy.max_turns != run.limits_snapshot_json.get("max_turns")
    ):
        raise BudgetError("Budget account requires a new Run with matching frozen limits")
    raw_cost = run.limits_snapshot_json.get("max_budget_usd")
    if (raw_cost is None) != (policy.max_cost_nanos is None):
        raise BudgetError("Budget cost policy does not match the frozen Run")
    if raw_cost is not None and usd_to_nanos(raw_cost) != policy.max_cost_nanos:
        raise BudgetError("Budget cost limit does not match the frozen Run")
    now = datetime.now(UTC)
    return RunBudgetAccount(
        id=uuid4(),
        run_id=run.id,
        policy_json=policy.to_json(),
        policy_checksum=budget_checksum(policy.to_json()),
        limits_checksum=budget_checksum(run.limits_snapshot_json),
        consumed_turns=Decimal(0),
        reserved_turns=Decimal(0),
        consumed_cost_nanos=Decimal(0) if policy.max_cost_nanos is not None else None,
        reserved_cost_nanos=Decimal(0) if policy.max_cost_nanos is not None else None,
        block_code=None,
        row_version=1,
        created_at=now,
        updated_at=now,
    )


class RunBudgetRepository(_RunRepositoryBase):
    """Run → Segment → Attempt → 勘定 → 預留の順に権限と共通残額を守る。"""

    async def _ledger(
        self,
        run: Run,
    ) -> tuple[RunBudgetAccount, BudgetPolicy, list[RunBudgetReservation]]:
        """Run lock 後だけ勘定とその預留を lock し、両者の総量を突き合わせる。"""

        account = (
            await self._session.scalars(
                select(RunBudgetAccount).where(RunBudgetAccount.run_id == run.id).with_for_update()
            )
        ).one_or_none()
        if account is None:
            raise BudgetUnavailableError("Run has no verified budget account")
        policy = BudgetPolicy.from_json(account.policy_json)
        if account.policy_checksum != budget_checksum(
            policy.to_json()
        ) or account.limits_checksum != budget_checksum(run.limits_snapshot_json):
            raise BudgetUnavailableError("Frozen budget policy is inconsistent")
        rows = list(
            (
                await self._session.scalars(
                    select(RunBudgetReservation)
                    .where(RunBudgetReservation.run_id == run.id)
                    .order_by(RunBudgetReservation.execution_key)
                    .with_for_update()
                )
            ).all()
        )
        for dimension in ("turns", "cost_nanos"):
            enabled = dimension == "turns" or policy.max_cost_nanos is not None
            for prefix in ("consumed", "reserved"):
                field = f"{prefix}_{dimension}"
                value = getattr(account, field)
                values = [getattr(row, field) for row in rows]
                if not enabled:
                    if value is not None or any(item is not None for item in values):
                        raise BudgetUnavailableError("Disabled budget dimension has a balance")
                elif value is None or _stored_units(value) != sum(
                    _stored_units(item) for item in values
                ):
                    raise BudgetUnavailableError("Budget account and reservations disagree")
        return account, policy, rows

    async def _authorize(
        self,
        run: Run,
        segment: RunSegment | None,
        attempt: RunAttempt,
        claimed: ClaimedRun,
    ) -> None:
        """全ての lock 待ちが終わった時刻で lease と取消を再検証する。"""

        self._validate_claimed_lease(attempt, claimed, now=datetime.now(UTC))
        if (
            run.project_id != claimed.project_id
            or run.status != RunStatus.RUNNING.value
            or claimed.run_segment_id is None
            or segment is None
            or segment.status != "RUNNING"
        ):
            raise LeaseValidationError("Run is not available for budgeted execution")
        await self._reject_cancelled_execution(run.id)

    async def reserve_group(
        self,
        claimed: ClaimedRun,
        *,
        group_key: str,
        requests: tuple[BudgetReservationRequest, ...],
        confirm_only: bool = False,
    ) -> tuple[BudgetExecutionRecord, ...]:
        """一つの元操作の全預留を同時に確保する。部分再送や途中の増額は拒否する。"""

        budget_key(group_key)
        if not requests or len({item.execution_key for item in requests}) != len(requests):
            raise BudgetError("Reservation group requires unique executions")
        parents = {item.parent_execution_key for item in requests}
        if len(parents) != 1 or (None in parents and len(requests) != 1):
            raise BudgetError("A group must be one primary or children of the same execution")
        ordered = sorted(requests, key=lambda item: item.execution_key)
        checksum = budget_checksum(
            {
                "run_id": str(claimed.run_id),
                "segment_id": str(claimed.run_segment_id),
                "attempt_id": str(claimed.run_attempt_id),
                "group_key": group_key,
                "requests": [item.to_json() for item in ordered],
            }
        )
        run, segment, attempt = await self._lock_claimed_execution(claimed)
        account, policy, rows = await self._ledger(run)
        await self._authorize(run, segment, attempt, claimed)
        existing = [row for row in rows if row.group_key == group_key]
        if existing:
            if len(existing) != len(ordered) or any(
                row.group_checksum != checksum for row in existing
            ):
                raise BudgetConflictError("Reservation group identity has different parameters")
            return tuple(self._execution(row) for row in existing)
        if confirm_only:
            raise BudgetUnavailableError("Original reservation group was not committed")
        if account.block_code:
            raise BudgetUnavailableError("Budget account requires reconciliation")
        by_key = {row.execution_key: row for row in rows}
        if None in parents and any(
            row.parent_reservation_id is None
            and row.status in {"RESERVED", "START_INTENT"}
            and row.stop_confirmed_at is None
            for row in rows
        ):
            raise BudgetUnavailableError("A primary execution is still reserved or running")
        for item in ordered:
            if item.execution_key in by_key:
                raise BudgetConflictError("Execution key belongs to another reservation group")
            if (item.cost_nanos is None) != (policy.max_cost_nanos is None):
                raise BudgetError("Reservation does not cover the enabled budget dimensions")
            if item.parent_execution_key is not None:
                parent = by_key.get(item.parent_execution_key)
                if (
                    parent is None
                    or parent.run_attempt_id != claimed.run_attempt_id
                    or parent.status != "START_INTENT"
                    or parent.stop_confirmed_at is not None
                    or parent.final_usage_at is not None
                ):
                    raise BudgetConflictError(
                        "Child reservation requires its running parent execution"
                    )
        turns = sum(item.turns for item in ordered)
        cost = sum(item.cost_nanos or 0 for item in ordered)
        if _stored_units(account.consumed_turns) + _stored_units(
            account.reserved_turns
        ) + turns > policy.max_turns or (
            policy.max_cost_nanos is not None
            and _stored_units(account.consumed_cost_nanos)
            + _stored_units(account.reserved_cost_nanos)
            + cost
            > policy.max_cost_nanos
        ):
            raise BudgetExhaustedError("Insufficient uncommitted Run budget for the whole group")
        now = datetime.now(UTC)
        added = []
        for item in ordered:
            row = RunBudgetReservation(
                id=uuid4(),
                run_id=run.id,
                run_segment_id=claimed.run_segment_id,
                run_attempt_id=claimed.run_attempt_id,
                execution_key=item.execution_key,
                group_key=group_key,
                group_checksum=checksum,
                request_checksum=budget_checksum(item.to_json()),
                parent_reservation_id=by_key[item.parent_execution_key].id
                if item.parent_execution_key
                else None,
                execution_lease_hash=lease_token_hash(claimed.lease_token),
                status="RESERVED",
                granted_turns=Decimal(item.turns),
                reserved_turns=Decimal(item.turns),
                consumed_turns=Decimal(0),
                granted_cost_nanos=Decimal(item.cost_nanos)
                if item.cost_nanos is not None
                else None,
                reserved_cost_nanos=Decimal(item.cost_nanos)
                if item.cost_nanos is not None
                else None,
                consumed_cost_nanos=Decimal(0) if item.cost_nanos is not None else None,
                turns_watermark=-1,
                cost_watermark=-1,
                start_intent_at=None,
                stop_confirmed_at=None,
                final_usage_at=None,
                reconcile_worker_id=None,
                reconcile_token_hash=None,
                reconcile_expires_at=None,
                created_at=now,
                updated_at=now,
            )
            self._session.add(row)
            added.append(row)
        account.reserved_turns = Decimal(_stored_units(account.reserved_turns) + turns)
        if account.reserved_cost_nanos is not None:
            account.reserved_cost_nanos = Decimal(_stored_units(account.reserved_cost_nanos) + cost)
        self._touch(account)
        await self._session.flush()
        return tuple(self._execution(row) for row in added)

    async def start_execution(self, claimed: ClaimedRun, *, execution_key: str) -> bool:
        """起動意図の初回 commit だけが True。読戻し/retry で再起動を許可しない。"""

        budget_key(execution_key)
        run, segment, attempt = await self._lock_claimed_execution(claimed)
        account, _, rows = await self._ledger(run)
        await self._authorize(run, segment, attempt, claimed)
        row = next((item for item in rows if item.execution_key == execution_key), None)
        if (
            row is None
            or row.run_attempt_id != claimed.run_attempt_id
            or row.run_segment_id != claimed.run_segment_id
            or not hmac.compare_digest(
                row.execution_lease_hash, lease_token_hash(claimed.lease_token)
            )
        ):
            raise LeaseValidationError("Reservation does not belong to this execution lease")
        if row.status == "START_INTENT":
            return False
        if row.status != "RESERVED" or account.block_code:
            raise BudgetUnavailableError("Reservation cannot start")
        if row.parent_reservation_id is not None:
            parent = next((item for item in rows if item.id == row.parent_reservation_id), None)
            if (
                parent is None
                or parent.status != "START_INTENT"
                or parent.stop_confirmed_at is not None
                or parent.final_usage_at is not None
            ):
                raise BudgetUnavailableError("Parent execution no longer permits child startup")
        row.status = "START_INTENT"
        row.start_intent_at = datetime.now(UTC)
        row.updated_at = row.start_intent_at
        self._touch(account)
        await self._session.flush()
        return True

    async def _reconciliation_rows(
        self,
        project_id: UUID,
        run_id: UUID,
    ) -> tuple[RunBudgetAccount, BudgetPolicy, list[RunBudgetReservation]]:
        """終態 Run も核対するが、Project 境界を越えず実行 lease を更新しない。"""

        run = await self._lock_run_row(run_id, project_id=project_id)
        if run is None or run.project_id != project_id:
            raise BudgetUnavailableError("Budget Run is not available")
        return await self._ledger(run)

    async def claim_reconciliation(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        execution_key: str,
        worker_id: str,
        token: str,
        lease_seconds: int = 60,
    ) -> BudgetReconciliationClaim:
        """核対専用 Worker の短期権限を発行する。元 Worker の token を権限源にしない。"""

        for key in (execution_key, worker_id, token):
            budget_key(key)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise BudgetError("Invalid reconciliation lease duration")
        _, _, rows = await self._reconciliation_rows(project_id, run_id)
        row = next((item for item in rows if item.execution_key == execution_key), None)
        if row is None:
            raise BudgetUnavailableError("Original reservation does not exist")
        now = datetime.now(UTC)
        if row.reconcile_expires_at is not None and row.reconcile_expires_at > now:
            if row.reconcile_worker_id != worker_id or not hmac.compare_digest(
                row.reconcile_token_hash or "", lease_token_hash(token)
            ):
                raise LeaseValidationError("Reservation is already being reconciled")
        else:
            row.reconcile_worker_id = worker_id
            row.reconcile_token_hash = lease_token_hash(token)
            row.reconcile_expires_at = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
        await self._session.flush()
        return BudgetReconciliationClaim(
            project_id, run_id, row.id, worker_id, token, row.reconcile_expires_at
        )

    async def _authorize_reconciliation(
        self,
        claim: BudgetReconciliationClaim,
    ) -> tuple[RunBudgetAccount, BudgetPolicy, RunBudgetReservation]:
        """元 Attempt が失効していても、現在の核対権だけで原預留を扱う。"""

        if not isinstance(claim, BudgetReconciliationClaim):
            raise LeaseValidationError("Budget settlement requires a reconciliation claim")
        account, policy, rows = await self._reconciliation_rows(claim.project_id, claim.run_id)
        row = next((item for item in rows if item.id == claim.reservation_id), None)
        if (
            row is None
            or row.reconcile_worker_id != claim.worker_id
            or not hmac.compare_digest(
                row.reconcile_token_hash or "", lease_token_hash(claim.token)
            )
            or row.reconcile_expires_at is None
            or row.reconcile_expires_at <= datetime.now(UTC)
        ):
            raise LeaseValidationError("Budget reconciliation lease is invalid")
        return account, policy, row

    async def _receipt(
        self,
        row: RunBudgetReservation,
        key: str,
    ) -> RunBudgetReceipt | None:
        """預留 lock の後で同じ報告鍵を照合する。"""

        return (
            await self._session.scalars(
                select(RunBudgetReceipt).where(
                    RunBudgetReceipt.reservation_id == row.id,
                    RunBudgetReceipt.receipt_key == key,
                )
            )
        ).one_or_none()

    async def _record_receipt(
        self,
        account: RunBudgetAccount,
        policy: BudgetPolicy,
        row: RunBudgetReservation,
        claim: BudgetReconciliationClaim,
        *,
        key: str,
        kind: str,
        payload: dict[str, object],
        disposition: str,
    ) -> BudgetReceiptResult:
        """報告と勘定の変更を同じ transaction で残し、例外で異常記録を消さない。"""

        if row.reconcile_expires_at is None or row.reconcile_expires_at <= datetime.now(UTC):
            raise LeaseValidationError("Reconciliation lease expired before receipt commit")
        self._session.add(
            RunBudgetReceipt(
                id=uuid4(),
                reservation_id=row.id,
                receipt_key=key,
                kind=kind,
                payload_json=payload,
                payload_checksum=budget_checksum({"kind": kind, "payload": payload}),
                reconcile_worker_id=claim.worker_id,
                disposition=disposition,
                created_at=datetime.now(UTC),
            )
        )
        row.updated_at = datetime.now(UTC)
        self._touch(account)
        await self._session.flush()
        return BudgetReceiptResult(
            self._account(account, policy), self._execution(row), disposition
        )

    async def _replay(
        self,
        account: RunBudgetAccount,
        policy: BudgetPolicy,
        row: RunBudgetReservation,
        claim: BudgetReconciliationClaim,
        *,
        key: str,
        kind: str,
        payload: dict[str, object],
    ) -> BudgetReceiptResult | None:
        """同鍵異内容は元報告を上書きせず、独立した衝突記録と勘定停止を commit する。"""

        existing = await self._receipt(row, key)
        if existing is None:
            return None
        checksum = budget_checksum({"kind": kind, "payload": payload})
        if existing.payload_checksum == checksum:
            return BudgetReceiptResult(
                self._account(account, policy), self._execution(row), "REPLAY"
            )
        account.block_code = "receipt_conflict"
        conflict_key = "conflict/" + budget_checksum({"key": key, "checksum": checksum})
        if await self._receipt(row, conflict_key) is None:
            return await self._record_receipt(
                account,
                policy,
                row,
                claim,
                key=conflict_key,
                kind="UNVERIFIABLE",
                payload={"original_key": key, "conflicting_checksum": checksum},
                disposition="CONFLICT",
            )
        return BudgetReceiptResult(self._account(account, policy), self._execution(row), "CONFLICT")

    async def record_usage(
        self,
        claim: BudgetReconciliationClaim,
        report: BudgetUsageReport,
    ) -> BudgetReceiptResult:
        """正常・失敗・終態後の原実行用量を一度だけ入帳し、未知分は占用したままにする。"""

        account, policy, row = await self._authorize_reconciliation(claim)
        payload = report.to_json()
        replay = await self._replay(
            account, policy, row, claim, key=report.report_key, kind="USAGE", payload=payload
        )
        if replay is not None:
            return replay
        invalid = None
        if (
            report.source != policy.source
            or report.metering_version != policy.metering_version
            or report.mode is not policy.mode
        ):
            invalid = "metering_identity_mismatch"
        if row.status in {"RESERVED", "RELEASED"}:
            invalid = "usage_without_start_intent"
        dimensions = [("turns", report.turns, "turns_watermark")]
        if policy.max_cost_nanos is not None:
            dimensions.append(("cost_nanos", report.cost_nanos, "cost_watermark"))
        deltas: list[tuple[str, int, str]] = []
        for dimension, value, watermark_field in dimensions:
            if value is None:
                continue
            consumed = int(getattr(row, f"consumed_{dimension}"))
            delta = value
            if report.mode is MeteringMode.CUMULATIVE:
                assert report.watermark is not None
                previous = getattr(row, watermark_field)
                if report.final and report.watermark < previous:
                    invalid = "stale_final_usage"
                if (report.watermark >= previous and value < consumed) or (
                    report.watermark <= previous and value > consumed
                ):
                    invalid = "cumulative_usage_conflict"
                delta = max(0, value - consumed)
            deltas.append((dimension, delta, watermark_field))
        if invalid:
            account.block_code = invalid
            return await self._record_receipt(
                account,
                policy,
                row,
                claim,
                key=report.report_key,
                kind="USAGE",
                payload=payload,
                disposition="INVALID",
            )
        overspent = False
        for dimension, delta, watermark_field in deltas:
            consumed_field, reserved_field = f"consumed_{dimension}", f"reserved_{dimension}"
            held = int(getattr(row, reserved_field))
            transfer = min(held, delta)
            setattr(
                row, consumed_field, Decimal(_stored_units(getattr(row, consumed_field)) + delta)
            )
            setattr(row, reserved_field, Decimal(held - transfer))
            setattr(
                account,
                consumed_field,
                Decimal(_stored_units(getattr(account, consumed_field)) + delta),
            )
            setattr(
                account,
                reserved_field,
                Decimal(_stored_units(getattr(account, reserved_field)) - transfer),
            )
            if report.watermark is not None:
                setattr(row, watermark_field, max(getattr(row, watermark_field), report.watermark))
            if getattr(row, consumed_field) > getattr(row, f"granted_{dimension}"):
                overspent = True
            if row.final_usage_at is not None and delta:
                account.block_code = "usage_after_final_report"
        if overspent:
            account.block_code = "budget_overspent"
        if report.final and all(value is not None for _, value, _ in dimensions):
            row.final_usage_at = row.final_usage_at or datetime.now(UTC)
        self._settle_if_confirmed(account, row)
        return await self._record_receipt(
            account,
            policy,
            row,
            claim,
            key=report.report_key,
            kind="USAGE",
            payload=payload,
            disposition="OVERSPENT" if overspent else "APPLIED",
        )

    async def confirm_stopped(
        self,
        claim: BudgetReconciliationClaim,
        *,
        receipt_key: str,
        verified_evidence: str,
    ) -> BudgetReceiptResult:
        """adapter を lock 外で核対した専用 Worker だけが停止根拠を渡す。用量は推定しない。"""

        budget_key(receipt_key)
        budget_key(verified_evidence)
        account, policy, row = await self._authorize_reconciliation(claim)
        payload: dict[str, object] = {"verified_evidence": verified_evidence}
        replay = await self._replay(
            account, policy, row, claim, key=receipt_key, kind="STOP", payload=payload
        )
        if replay is not None:
            return replay
        if row.start_intent_at is None:
            raise BudgetError("An unstarted reservation requires the no-start release path")
        row.stop_confirmed_at = row.stop_confirmed_at or datetime.now(UTC)
        self._settle_if_confirmed(account, row)
        return await self._record_receipt(
            account,
            policy,
            row,
            claim,
            key=receipt_key,
            kind="STOP",
            payload=payload,
            disposition="APPLIED",
        )

    async def release_unstarted(
        self,
        claim: BudgetReconciliationClaim,
        *,
        receipt_key: str,
    ) -> BudgetReceiptResult:
        """起動意図がないことを同じ lock 下で確定し、将来の起動を不可逆に閉じる。"""

        budget_key(receipt_key)
        account, policy, row = await self._authorize_reconciliation(claim)
        payload: dict[str, object] = {"reason": "start_intent_absent"}
        replay = await self._replay(
            account, policy, row, claim, key=receipt_key, kind="UNSTARTED", payload=payload
        )
        if replay is not None:
            return replay
        if (
            account.block_code
            or row.start_intent_at is not None
            or row.status not in {"RESERVED", "RELEASED"}
        ):
            raise BudgetError("Start intent may already have caused billable execution")
        self._release(account, row)
        row.status = "RELEASED"
        return await self._record_receipt(
            account,
            policy,
            row,
            claim,
            key=receipt_key,
            kind="UNSTARTED",
            payload=payload,
            disposition="APPLIED",
        )

    async def mark_unverifiable(
        self,
        claim: BudgetReconciliationClaim,
        *,
        receipt_key: str,
        evidence_checksum: str,
    ) -> BudgetReceiptResult:
        """数値として受理できない報告も占用を残し、後続の新規消費を止める。"""

        budget_key(receipt_key)
        budget_key(evidence_checksum)
        account, policy, row = await self._authorize_reconciliation(claim)
        payload: dict[str, object] = {"evidence_checksum": evidence_checksum}
        replay = await self._replay(
            account, policy, row, claim, key=receipt_key, kind="UNVERIFIABLE", payload=payload
        )
        if replay is not None:
            return replay
        account.block_code = "usage_unverifiable"
        return await self._record_receipt(
            account,
            policy,
            row,
            claim,
            key=receipt_key,
            kind="UNVERIFIABLE",
            payload=payload,
            disposition="INVALID",
        )

    @staticmethod
    def _release(account: RunBudgetAccount, row: RunBudgetReservation) -> None:
        """確認された未使用分だけを解放し、消費を減額しない。"""

        account.reserved_turns = Decimal(
            _stored_units(account.reserved_turns) - _stored_units(row.reserved_turns)
        )
        row.reserved_turns = Decimal(0)
        if row.reserved_cost_nanos is not None:
            assert account.reserved_cost_nanos is not None
            account.reserved_cost_nanos = Decimal(
                _stored_units(account.reserved_cost_nanos) - _stored_units(row.reserved_cost_nanos)
            )
            row.reserved_cost_nanos = Decimal(0)

    @classmethod
    def _settle_if_confirmed(cls, account: RunBudgetAccount, row: RunBudgetReservation) -> None:
        """停止と最終用量の両方が揃うまでは、取消/終態でも未決占用を保つ。"""

        if row.stop_confirmed_at is not None and row.final_usage_at is not None:
            cls._release(account, row)
            row.status = "SETTLED"

    @staticmethod
    def _touch(account: RunBudgetAccount) -> None:
        """勘定の変更時点と並行版を単一の意味で進める。"""

        account.row_version += 1
        account.updated_at = datetime.now(UTC)

    @staticmethod
    def _execution(row: RunBudgetReservation) -> BudgetExecutionRecord:
        """数値を厳密整数へ投影し、内部 lease token は外へ渡さない。"""

        return BudgetExecutionRecord(
            row.id,
            row.execution_key,
            BudgetExecutionStatus(row.status),
            int(row.granted_turns),
            int(row.reserved_turns),
            int(row.consumed_turns),
            int(row.granted_cost_nanos) if row.granted_cost_nanos is not None else None,
            int(row.reserved_cost_nanos) if row.reserved_cost_nanos is not None else None,
            int(row.consumed_cost_nanos) if row.consumed_cost_nanos is not None else None,
            row.stop_confirmed_at is not None,
            row.final_usage_at is not None,
        )

    @staticmethod
    def _account(account: RunBudgetAccount, policy: BudgetPolicy) -> BudgetAccountRecord:
        """未知 cost と既知のゼロを区別した動的 read model を返す。"""

        return BudgetAccountRecord(
            account.run_id,
            policy,
            int(account.consumed_turns),
            int(account.reserved_turns),
            int(account.consumed_cost_nanos) if account.consumed_cost_nanos is not None else None,
            int(account.reserved_cost_nanos) if account.reserved_cost_nanos is not None else None,
            account.block_code,
            account.row_version,
        )
