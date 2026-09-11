"""主 Run の預留と Engine callback を同じ実行 scope に結ぶ。"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Protocol

from skillmind.agent.domain import RunContext
from skillmind.agent.metering import AgentInvocation, ResultUsageObservation
from skillmind.core.cancellation import check_pending_cancellation
from skillmind.runs.budget import (
    BudgetExecutionRecord,
    BudgetExecutionStatus,
    BudgetPolicy,
    BudgetUnavailableError,
)
from skillmind.runs.budget_execution import BudgetInvocationRecorder, InvocationBudgetStore
from skillmind.runs.domain import ClaimedRun


class PrimaryBudgetStore(InvocationBudgetStore, Protocol):
    """原 Run 帳簿の A と、既存 recorder の束縛/B/観測を提供する。"""

    async def reserve_primary(
        self, claimed: ClaimedRun, *, expected_policy: BudgetPolicy
    ) -> BudgetExecutionRecord:
        """原主 Attempt の残額授与を commit 後にだけ返す。"""
        ...


@dataclass(frozen=True, slots=True)
class PreparedPrimaryBudget:
    """実行局部限額と一回限りの callback owner。Agent へ recorder を公開しない。"""

    context: RunContext
    recorder: BudgetInvocationRecorder


class PrimaryBudgetCoordinator:
    """信頼した adapter policy を持つ装配向け。自身では SDK 計量を認証しない。

    主 Engine の before_connect/usage_observer と Executor の両方へ同じ instance を
    渡す。未知・取消・終態を観測しただけでは、占用を解放したり用量を補造しない。
    """

    def __init__(self, store: PrimaryBudgetStore, *, policy: BudgetPolicy) -> None:
        """profile の検証を呼出し元へ要求し、未接続の金銭 adapter は拒否する。"""
        if policy.max_cost_nanos is not None:
            raise BudgetUnavailableError("Primary execution cost adapter is not configured")
        self._store = store
        self._policy = policy
        self._active: ContextVar[BudgetInvocationRecorder | None] = ContextVar(
            "primary_budget_recorder", default=None
        )

    async def prepare(self, claimed: ClaimedRun, context: RunContext) -> PreparedPrimaryBudget:
        """元 identity/上限を検証して A を確定し、SDK 用の局部上限だけを縮小する。"""
        await check_pending_cancellation()
        if (
            context.run_id != claimed.run_id
            or context.run_attempt_id != claimed.run_attempt_id
            or context.project_id != claimed.project_id
            or context.user_id != claimed.actor_id
            or context.limits.max_turns != self._policy.max_turns
            or context.limits.max_budget_usd is not None
            or claimed.limits_snapshot_json.get("max_turns") != self._policy.max_turns
            or claimed.limits_snapshot_json.get("max_budget_usd") is not None
        ):
            raise BudgetUnavailableError("Primary context does not match its frozen budget policy")
        try:
            grant = await self._store.reserve_primary(claimed, expected_policy=self._policy)
        finally:
            await check_pending_cancellation()
        execution_key = f"primary:{claimed.run_attempt_id}"
        if (
            grant.execution_key != execution_key
            or grant.status is not BudgetExecutionStatus.RESERVED
            or type(grant.granted_turns) is not int
            or not 0 < grant.granted_turns <= self._policy.max_turns
            or grant.reserved_turns != grant.granted_turns
            or grant.consumed_turns != 0
            or grant.stop_confirmed
            or grant.final_usage_confirmed
            or grant.granted_cost_nanos is not None
        ):
            raise BudgetUnavailableError("Primary reservation is not available for a new start")
        # Brief/Run snapshot は認可上限のまま保ち、実 options は残額授与まで収窄する。
        # 同 Segment の再試行で凍結 Brief を書換えず、実 options は Session と invocation に残す。
        local = replace(context, limits=replace(context.limits, max_turns=grant.granted_turns))
        return PreparedPrimaryBudget(
            local,
            BudgetInvocationRecorder(
                self._store,
                claimed,
                execution_key=execution_key,
                reconciliation_worker_id=f"primary:{claimed.run_attempt_id}",
                reconciliation_token=f"reconcile:{secrets.token_urlsafe(32)}",
            ),
        )

    @contextmanager
    def bind(self, prepared: PreparedPrimaryBudget) -> Iterator[None]:
        """anext の子 task へ同じ recorder を継承し、別 Run の callback と混同しない。"""
        if self._active.get() is not None:
            raise BudgetUnavailableError("Nested primary budget execution is not supported")
        token = self._active.set(prepared.recorder)
        try:
            yield
        finally:
            self._active.reset(token)

    async def before_connect(self, invocation: AgentInvocation) -> bool:
        """原 scope の recorder が確認した一回だけの B を Engine に返す。"""
        return await self._recorder().before_connect(invocation)

    async def observe_result(self, observation: ResultUsageObservation) -> None:
        """原 SDK Result を同じ預留へ保存し、成功表示より先に保存失敗を伝える。"""
        await self._recorder().observe_result(observation)

    def _recorder(self) -> BudgetInvocationRecorder:
        """無装配の callback を無料のモデル起動として通過させない。"""
        recorder = self._active.get()
        if recorder is None:
            raise BudgetUnavailableError("Primary budget execution scope is missing")
        return recorder
