"""予算の A/B/C 提交を短い transaction に閉じ、commit 成功前に起動許可を返さない。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.runs.budget import (
    BudgetExecutionRecord,
    BudgetReceiptResult,
    BudgetReconciliationClaim,
    BudgetReservationRequest,
    BudgetStartUncertainError,
    BudgetUsageReport,
)
from projectmind.runs.domain import ClaimedRun
from projectmind.runs.repository_budgets import RunBudgetRepository


class PostgresRunBudgetStore:
    """予約・起動意図・専用核対の transaction 所有者。SDK/外部 I/O は受け付けない。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """それぞれの提交で独立した session を確保する。"""

        self._session_factory = session_factory

    async def reserve_group(
        self,
        claimed: ClaimedRun,
        *,
        group_key: str,
        requests: tuple[BudgetReservationRequest, ...],
    ) -> tuple[BudgetExecutionRecord, ...]:
        """A の応答喪失は原鍵の読戻しだけで解決し、追加の預留を作らない。"""

        records: tuple[BudgetExecutionRecord, ...] | None = None
        try:
            async with self._session_factory() as session, session.begin():
                records = await RunBudgetRepository(session).reserve_group(
                    claimed,
                    group_key=group_key,
                    requests=requests,
                )
        except (DBAPIError, TimeoutError, ConnectionError):
            if records is None:
                raise
            async with self._session_factory() as session, session.begin():
                return await RunBudgetRepository(session).reserve_group(
                    claimed,
                    group_key=group_key,
                    requests=requests,
                    confirm_only=True,
                )
        return records

    async def start_execution(self, claimed: ClaimedRun, *, execution_key: str) -> bool:
        """B の初回成功だけが True。成否不明な B を retry して許可に変換しない。"""

        try:
            async with self._session_factory() as session, session.begin():
                return await RunBudgetRepository(session).start_execution(
                    claimed,
                    execution_key=execution_key,
                )
        except (DBAPIError, TimeoutError, ConnectionError) as error:
            raise BudgetStartUncertainError("Budget start intent could not be confirmed") from error

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
        """元鍵/token を保った再送は既存の核対権を確認し、世代を無制限に更新しない。"""

        async with self._session_factory() as session, session.begin():
            return await RunBudgetRepository(session).claim_reconciliation(
                project_id=project_id,
                run_id=run_id,
                execution_key=execution_key,
                worker_id=worker_id,
                token=token,
                lease_seconds=lease_seconds,
            )

    async def record_usage(
        self,
        claim: BudgetReconciliationClaim,
        report: BudgetUsageReport,
    ) -> BudgetReceiptResult:
        """C は報告/勘定を同時 commit する。同鍵異内容の異常結果も rollback しない。"""

        async with self._session_factory() as session, session.begin():
            return await RunBudgetRepository(session).record_usage(claim, report)

    async def confirm_stopped(
        self,
        claim: BudgetReconciliationClaim,
        *,
        receipt_key: str,
        verified_evidence: str,
    ) -> BudgetReceiptResult:
        """lock 外で完了した adapter 核対の根拠だけを保存する。"""

        async with self._session_factory() as session, session.begin():
            return await RunBudgetRepository(session).confirm_stopped(
                claim,
                receipt_key=receipt_key,
                verified_evidence=verified_evidence,
            )

    async def release_unstarted(
        self,
        claim: BudgetReconciliationClaim,
        *,
        receipt_key: str,
    ) -> BudgetReceiptResult:
        """未起動の閉鎖と原占用の解放を同じ transaction で保存する。"""

        async with self._session_factory() as session, session.begin():
            return await RunBudgetRepository(session).release_unstarted(
                claim, receipt_key=receipt_key
            )

    async def mark_unverifiable(
        self,
        claim: BudgetReconciliationClaim,
        *,
        receipt_key: str,
        evidence_checksum: str,
    ) -> BudgetReceiptResult:
        """不正/未知の報告で新規消費を停止してから呼出し側へ結果を返す。"""

        async with self._session_factory() as session, session.begin():
            return await RunBudgetRepository(session).mark_unverifiable(
                claim,
                receipt_key=receipt_key,
                evidence_checksum=evidence_checksum,
            )
