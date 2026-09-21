"""自動承認可能な一操作を、原 SDK Tool の生存中に同じ Effect executor へ渡す。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from skillmind.agent.domain import RunContext
from skillmind.effects.inline import InlineEffectResult
from skillmind.runs.service import RunService
from skillmind.worker.effects import ApprovedEffectExecutor
from skillmind.worker.tool_authority import ToolExecutionAuthority


class InlineEffectCoordinator:
    """短い直接交付だけを試す。失敗/長時間/未知は元提案のまま延期する。"""

    def __init__(
        self,
        service: RunService,
        executor: ApprovedEffectExecutor,
        context: RunContext,
        authority: ToolExecutionAuthority,
    ) -> None:
        """SDK が所有する原 Run/Attempt を捕捉し、Secret や model の指定を受け取らない。"""
        self._service, self._executor = service, executor
        self._context, self._authority = context, authority

    async def invoke(
        self, arguments: Mapping[str, Any], call_id: str, session_id: str
    ) -> InlineEffectResult | None:
        """None は操作未作成の通常承認待ち。作成後の失敗を新要求へ変換しない。"""
        self._authority.require_active()
        claimed = self._authority.claimed
        # 長い Run を一 Attempt に詰めて時間切れにしない。送信/提案作成の前に
        # 既存 Effect 上限と交付余量を予約できなければ通常の継続へ戻す。
        deadline = self._authority.deadline
        if deadline is not None and (
            deadline - asyncio.get_running_loop().time() <= self._executor.wall_timeout_seconds + 30
        ):
            return None
        prepared = await self._service.begin_inline_effect(
            claimed, arguments=arguments, tool_use_id=call_id, session_id=session_id
        )
        if prepared is None:
            return None
        proposal_id, effect_id = prepared
        try:
            # 外部処理の期限は既存 Effect supervisor と Run deadline に従う。
            # 最適化のために短い別 timeout を設けて正常な操作を未知にしない。
            await self._executor.execute(effect_id, inline_parent=claimed)
            self._authority.require_active()
            receipt = await self._service.inline_effect_receipt(claimed, proposal_id)
            return InlineEffectResult(proposal_id, receipt)
        except asyncio.CancelledError:
            # 親取消・job停止は上位へ伝える。既送操作は原台帳から回収する。
            raise
        except Exception:
            # I/O/監督失敗の本文は model へ出さない。native 停止後に原 operation を延期する。
            return InlineEffectResult(proposal_id)
