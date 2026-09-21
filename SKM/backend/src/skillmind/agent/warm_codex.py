"""一つの Queue job の確認済み続行間だけ Codex transport を再利用する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from uuid import UUID

from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import ThreadUnsubscribeResponse


class WarmCodexSession:
    """権限/Tool server は保持せず、終端確認後の SDK process だけを短期所有する。"""

    def __init__(self, run_id: UUID, owner: object) -> None:
        """元 Run と Engine instance を固定し、他 job や fork に貸し出さない。"""
        self.run_id, self.owner = run_id, owner
        self.client: CodexClient | None = None
        self.active = True
        self.busy = False
        self.session_id: str | None = None

    async def acquire(
        self, factory: Callable[[], CodexClient], parent: str | None
    ) -> tuple[CodexClient, bool]:
        """既知の原親以外は共有せず、新 model 呼出しより前に cold path へ戻す。"""
        if not self.active or self.busy:
            raise RuntimeError("Warm Codex scope is unavailable")
        if self.client is not None and parent != self.session_id:
            await self.close_client()
        is_new = self.client is None
        if is_new:
            self.client = factory()
        assert self.client is not None
        self.busy = True
        return self.client, is_new

    async def release(self, client: CodexClient, session_id: str | None, *, terminal: bool) -> None:
        """終端済み thread の購読を解除し、新 Attempt の別 MCP endpoint を使える状態へする。"""
        if client is not self.client:
            raise RuntimeError("Warm Codex client identity changed")
        self.busy = False
        if not terminal or session_id is None or not self.active:
            await self.close_client()
            return

        # 固定 SDK 0.154 の public request を使用。unsubscribe は thread/history の削除でない。
        def unsubscribe() -> ThreadUnsubscribeResponse:
            """SDK の generic keyword を同じ同期 call 内で束縛する。"""
            return client.request(
                "thread/unsubscribe",
                {"threadId": session_id},
                response_model=ThreadUnsubscribeResponse,
            )

        request = asyncio.create_task(asyncio.to_thread(unsubscribe))
        try:
            response = await asyncio.wait_for(asyncio.shield(request), timeout=5)
            status = response.model_dump(mode="json")["status"]
            if status not in {"unsubscribed", "notLoaded"}:
                await self.close_client()
            else:
                self.session_id = session_id
        except asyncio.CancelledError:
            await self.close_client()
            raise
        except Exception:
            # 暖機の失敗は新規 model 再試行で補わない。原終端は保存済みで次回は通常 resume。
            await self.close_client()
        finally:
            if not request.done():
                await self.close_client()
            with suppress(Exception, asyncio.CancelledError):
                await request

    async def close_client(self) -> None:
        """専用 process だけを回収し、共有認証・原 thread・業務記録は変更しない。"""
        client, self.client = self.client, None
        self.session_id = None
        self.busy = False
        if client is not None:
            await asyncio.to_thread(client.close)


_CURRENT: ContextVar[WarmCodexSession | None] = ContextVar("warm_codex", default=None)


def current_warm_session(run_id: UUID, owner: object) -> WarmCodexSession | None:
    """派生 task が scope 終了後や別 Run へ古い client を持ち込むことを拒否する。"""
    value = _CURRENT.get()
    if value is None or not value.active or value.run_id != run_id or value.owner is not owner:
        return None
    return value


@asynccontextmanager
async def warm_codex_scope(run_id: UUID, owner: object) -> AsyncIterator[None]:
    """Worker の有限な連続処理だけで有効。人工待ちや job 終了時に必ず閉じる。"""
    session = WarmCodexSession(run_id, owner)
    token = _CURRENT.set(session)
    try:
        yield
    finally:
        session.active = False
        _CURRENT.reset(token)
        await session.close_client()
