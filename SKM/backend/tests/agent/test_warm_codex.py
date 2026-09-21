"""原 terminal に限定した SDK 再利用と失敗時の解放を検証する。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from skillmind.agent.warm_codex import WarmCodexSession, current_warm_session, warm_codex_scope


class Client:
    """実モデルを呼ばず、購読解除と process close を記録する。"""

    def __init__(self, status="unsubscribed"):
        """終了状態を固定し、各呼出しを保持する。"""
        self.status, self.closed, self.calls = status, False, []

    def request(self, method, params, **kwargs):
        """公開 SDK request の応答形状を返す。"""
        self.calls.append((method, params))
        if isinstance(self.status, Exception):
            raise self.status
        return SimpleNamespace(model_dump=lambda **_: {"status": self.status})

    def close(self):
        """専用 process の終了だけを模擬する。"""
        self.closed = True


async def test_warm_client_requires_matching_parent_and_single_owner():
    """原終端確認後だけ同じ親へ貸し出し、不一致と多重取得を拒否する。"""
    holder = WarmCodexSession(uuid4(), object())
    client, first = await holder.acquire(Client, None)
    assert first
    with pytest.raises(RuntimeError):
        await holder.acquire(Client, None)
    await holder.release(client, "thread-a", terminal=True)
    same, new = await holder.acquire(Client, "thread-a")
    assert same is client and not new
    await holder.release(same, "thread-a", terminal=True)
    other, new = await holder.acquire(Client, "thread-b")
    assert new and other is not client and client.closed
    await holder.release(other, "thread-b", terminal=False)
    assert other.closed and holder.client is None


@pytest.mark.parametrize("status", ["notSubscribed", ValueError("fixture")])
async def test_unsubscribe_failure_closes_and_next_acquisition_is_cold(status):
    """暖機の失敗を新 model 呼出しに変えず、次回は元の cold 起動を使う。"""
    holder = WarmCodexSession(uuid4(), object())
    client, _ = await holder.acquire(lambda: Client(status), None)
    await holder.release(client, "thread", terminal=True)
    assert client.closed and holder.client is None
    next_client, new = await holder.acquire(Client, "thread")
    assert new
    await holder.close_client()
    assert next_client.closed


async def test_scope_does_not_leak_run_engine_or_client():
    """派生 context に残っても失効した所有者を再利用しない。"""
    run, engine = uuid4(), object()
    async with warm_codex_scope(run, engine):
        holder = current_warm_session(run, engine)
        assert holder is not None
        assert current_warm_session(uuid4(), engine) is None
        assert current_warm_session(run, object()) is None
        client, _ = await holder.acquire(Client, None)
    assert current_warm_session(run, engine) is None
    assert client.closed and not holder.active
    with pytest.raises(RuntimeError):
        await holder.acquire(Client, None)
