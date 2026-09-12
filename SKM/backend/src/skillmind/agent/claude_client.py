"""固定 SDK の disconnect に、所有する in-process Tool task の終了待機を加える。"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk._internal._task_compat import TaskHandle
from claude_agent_sdk._internal.query import Query
from claude_agent_sdk._internal.transport import Transport

from skillmind.core.cancellation import check_pending_cancellation


class DrainingClaudeClient(ClaudeSDKClient):
    """CLI だけでなく SDK 所有 task の終了を待つ。外部停止や課金完了は保証しない。"""

    def __init__(
        self,
        options: ClaudeAgentOptions,
        transport: Transport | None = None,
    ) -> None:
        """既定 SDK の接続・SessionStore 処理を保ち、close の所有者を直列化する。"""
        super().__init__(options, transport)
        self._disconnect_lock = asyncio.Lock()
        self._pending_children: set[TaskHandle] = set()
        self._closed_query: Query | None = None
        self._child_cleanup_failed = False

    async def disconnect(self) -> None:
        """成功時は全子 task を回収し、中断時は元 handle を次の close へ保持する。"""
        try:
            async with self._disconnect_lock:
                await self._disconnect_owned()
        finally:
            # 既に届いた取消しは cleanup の途中で再送せず、全解放の後で伝播する。
            # 各 await 自体が新しい取消しを返す場合は元 handle を残して退出する。
            await check_pending_cancellation()

    async def _disconnect_owned(self) -> None:
        """一つの close 所有者だけで transport・子 task・入力の順に解放する。"""
        # SDK 0.2.110 は Query.close で子へ cancel するが join しない。close 開始時
        # に _closed を設定するので、この snapshot 後に新 control handler は増えない。
        query: Query | None = self._query
        if query is not None:
            self._pending_children.update(query._child_tasks)
            if query is not self._closed_query:
                await query.close()
                self._closed_query = query
        elif self._transport is not None:
            # Transport.connect 後、Query 作成前の失敗も process を所有している。
            # SDK の素の disconnect はこの transport を close せず参照を消す。
            await self._transport.close()
        children = tuple(self._pending_children)
        outcomes = await asyncio.gather(
            *(_wait_for_child(child) for child in children),
            return_exceptions=True,
        )
        for child in children:
            self._pending_children.discard(child)
        if any(isinstance(outcome, BaseException) for outcome in outcomes):
            self._child_cleanup_failed = True
        if query is not None:
            query.close_receive_stream()
            self._query = None
            self._closed_query = None
        # SessionStore の一時入力は、参照中の Tool 後処理が終わった後に SDK へ解放させる。
        await super().disconnect()
        if self._child_cleanup_failed:
            # 各子の終了まで待ってから失敗を返す。Tool 本文や元例外は外へ出さない。
            raise RuntimeError("Claude SDK child cleanup failed") from None


async def _wait_for_child(child: TaskHandle) -> None:
    """待機者の取消しで、既に取消し後処理中の SDK task を二重 cancel しない。"""
    if not child.done():
        finished = asyncio.Event()
        child.add_done_callback(lambda _child: finished.set())
        await finished.wait()
    # 既に終わった task の例外だけ回収する。TaskHandle.wait は子の CancelledError を
    # 抑制するため、呼出し側の取消しは別 checkpoint で必ず伝播させる。
    try:
        await child.wait()
    finally:
        await check_pending_cancellation()
