"""原 invocation の束縛、初回起動許可、原観測保存を同じ実行へ接続する。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from projectmind.core.cancellation import check_pending_cancellation
from projectmind.runs.budget import (
    BudgetConflictError,
    BudgetInvocationBinding,
    BudgetReceiptResult,
    BudgetReconciliationClaim,
    BudgetUnavailableError,
    budget_key,
)
from projectmind.runs.domain import ClaimedRun

if TYPE_CHECKING:
    from projectmind.agent.metering import AgentInvocation, ResultUsageObservation


class InvocationBudgetStore(Protocol):
    """既存 store の短い transaction を使い、ここで別の帳簿を作らない。"""

    async def bind_invocation(
        self, claimed: ClaimedRun, *, execution_key: str, invocation: AgentInvocation
    ) -> BudgetInvocationBinding:
        """原預留へ完全な実行記述子を固定し、commit 後だけ返す。"""

        ...

    async def start_execution(
        self,
        claimed: ClaimedRun,
        *,
        execution_key: str,
        expected_invocation_id: UUID,
        expected_invocation_checksum: str,
    ) -> bool:
        """初回の明確な B commit のみ True を返す。"""

        ...

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
        """実行権とは独立した原預留の核対権を取得する。"""

        ...

    async def record_observation(
        self, claim: BudgetReconciliationClaim, observation: ResultUsageObservation
    ) -> BudgetReceiptResult:
        """未正規化の原観測を保存し、消費・停止・残額には変換しない。"""

        ...


class BudgetInvocationRecorder:
    """信頼した実行調整者が一預留ごとに組み立てる Engine callback の所有者。

    Profile/局部強制の検証や A の全組預留は先行条件。この部品の導入だけで新しい予算
    policy を有効にしない。Tool/公開 API から store やこの callback を供給させない。
    """

    def __init__(
        self,
        store: InvocationBudgetStore,
        claimed: ClaimedRun,
        *,
        execution_key: str,
        reconciliation_worker_id: str,
        reconciliation_token: str,
    ) -> None:
        """原実行鍵と専用核対 identity を固定し、再起動許可をキャッシュしない。"""

        self._store = store
        self._claimed = claimed
        self._execution_key = budget_key(execution_key)
        self._worker_id = budget_key(reconciliation_worker_id)
        self._token = budget_key(reconciliation_token)
        self._attempted = False
        self._started = False
        self._invocation: AgentInvocation | None = None
        self._binding: BudgetInvocationBinding | None = None

    async def before_connect(self, invocation: AgentInvocation) -> bool:
        """束縛の commit → B commit の順を守り、未知/再読取で起動を繰り返さない。"""

        if self._attempted:
            raise BudgetUnavailableError("Invocation start gate has already been used")
        self._attempted = True
        await check_pending_cancellation()
        try:
            binding = await self._store.bind_invocation(
                self._claimed,
                execution_key=self._execution_key,
                invocation=invocation,
            )
        finally:
            # 依存先が取消しを別の例外へ変換しても、次の B や再送へ進ませない。
            await check_pending_cancellation()
        if (
            binding.invocation_id != invocation.invocation_id
            or binding.invocation_checksum != invocation.checksum
        ):
            raise BudgetConflictError("Committed invocation binding does not match its request")
        self._invocation, self._binding = invocation, binding
        try:
            started = await self._store.start_execution(
                self._claimed,
                execution_key=self._execution_key,
                expected_invocation_id=binding.invocation_id,
                expected_invocation_checksum=binding.invocation_checksum,
            )
        finally:
            await check_pending_cancellation()
        self._started = started is True
        return self._started

    async def observe_result(self, observation: ResultUsageObservation) -> None:
        """同じ原束縛の観測だけを核対権で保存し、commit 後の衝突も成功へ隠さない。"""

        binding = self._binding
        await check_pending_cancellation()
        if not self._started or binding is None or observation.invocation != self._invocation:
            raise BudgetUnavailableError("Observation has no matching confirmed invocation start")
        try:
            claim = await self._store.claim_reconciliation(
                project_id=self._claimed.project_id,
                run_id=self._claimed.run_id,
                execution_key=self._execution_key,
                worker_id=self._worker_id,
                token=self._token,
            )
        finally:
            await check_pending_cancellation()
        if (
            claim.reservation_id != binding.reservation_id
            or claim.project_id != self._claimed.project_id
            or claim.run_id != self._claimed.run_id
            or claim.worker_id != self._worker_id
        ):
            raise BudgetConflictError("Reconciliation does not belong to the bound reservation")
        try:
            result = await self._store.record_observation(claim, observation)
        finally:
            # 保存済みの事実は取り消さないが、旧 task を正常完了扱いには戻さない。
            await check_pending_cancellation()
        if result.disposition == "CONFLICT":
            # 異常の transaction は既に commit 済み。成功 Result を遮断して監査を残す。
            raise BudgetConflictError("Raw invocation observations require reconciliation")
        if result.disposition not in {"OBSERVED", "REPLAY"}:
            raise BudgetUnavailableError("Raw invocation observation was not confirmed")
