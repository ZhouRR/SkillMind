"""固定 SDK task の清理を drain し、入力解放と取消しの順序を検証する。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal._task_compat import spawn_detached

from skillmind.agent.claude_client import DrainingClaudeClient


def client_with_query() -> tuple[DrainingClaudeClient, SimpleNamespace, AsyncMock]:
    """SDK close の子取消しと materialization 解放を独立して観測する。"""
    client = DrainingClaudeClient(ClaudeAgentOptions())
    query = SimpleNamespace(
        _child_tasks=set(),
        close=AsyncMock(),
        close_receive_stream=MagicMock(),
    )
    client._query = query
    release = AsyncMock()
    client._materialized = SimpleNamespace(cleanup=release)
    return client, query, release


async def test_disconnect_waits_for_children_before_releasing_input() -> None:
    """CLI close が返っても、Tool の finally 完了までは一時入力を残す。"""
    client, query, release = client_with_query()
    running, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def tool() -> None:
        """取消し後も、明示的に許可するまで入力を参照する清理を保留する。"""
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()

    child = spawn_detached(tool())
    query._child_tasks.add(child)
    query.close.side_effect = child.cancel
    await running.wait()
    closing = asyncio.create_task(client.disconnect())
    try:
        await cleaning.wait()
        assert not closing.done()
        release.assert_not_awaited()
        finish.set()
        await closing
        assert child.done()
        release.assert_awaited_once()
        query.close.assert_awaited_once()
        query.close_receive_stream.assert_called_once()
    finally:
        finish.set()
        await asyncio.gather(closing, return_exceptions=True)


async def test_cancelled_drain_keeps_original_child_and_does_not_cancel_it_twice() -> None:
    """close 呼出し側の取消しは子の清理を切らず、次の close が同じ handle を待つ。"""
    client, query, release = client_with_query()
    running, cleaning, finish, cleaned = (asyncio.Event() for _ in range(4))

    async def tool() -> None:
        """二回目の cancel なら cleaned に到達しない、取消し敏感な清理を持つ。"""
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()
            cleaned.set()

    child = spawn_detached(tool())
    query._child_tasks.add(child)
    query.close.side_effect = child.cancel
    await running.wait()
    closing = asyncio.create_task(client.disconnect())
    try:
        await cleaning.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not child.done()
        release.assert_not_awaited()
        retry = asyncio.create_task(client.disconnect())
        finish.set()
        await retry
        assert cleaned.is_set()
        query.close.assert_awaited_once()
        release.assert_awaited_once()
    finally:
        finish.set()
        await asyncio.gather(closing, child.wait(), return_exceptions=True)


async def test_concurrent_disconnect_has_one_sdk_close_owner() -> None:
    """中断側と受信側が同時に disconnect しても SDK close を重ねない。"""
    client, query, release = client_with_query()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def close() -> None:
        """最初の SDK close を保留し、その間の第二呼出しを待たせる。"""
        entered.set()
        await finish.wait()

    query.close.side_effect = close
    first = asyncio.create_task(client.disconnect())
    await entered.wait()
    second = asyncio.create_task(client.disconnect())
    finish.set()
    await asyncio.gather(first, second)
    query.close.assert_awaited_once()
    release.assert_awaited_once()


async def test_child_failure_waits_for_siblings_and_is_not_forgotten_on_reclose() -> None:
    """一子の失敗で他の清理を打切らず、再 close も失敗を成功へ塗り替えない。"""
    client, query, release = client_with_query()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def failed() -> None:
        """外へ漏らさない例外を一件作る。"""
        raise RuntimeError("fixture-private-tool-body")

    async def sibling() -> None:
        """兄弟が完了するまでは元 close を成功にも失敗にも戻さない。"""
        entered.set()
        await finish.wait()

    query._child_tasks.update({spawn_detached(failed()), spawn_detached(sibling())})
    closing = asyncio.create_task(client.disconnect())
    try:
        await entered.wait()
        assert not closing.done()
        finish.set()
        with pytest.raises(RuntimeError, match=r"^Claude SDK child cleanup failed$"):
            await closing
        release.assert_awaited_once()
        with pytest.raises(RuntimeError, match=r"^Claude SDK child cleanup failed$"):
            await client.disconnect()
        query.close.assert_awaited_once()
    finally:
        finish.set()
        await asyncio.gather(closing, return_exceptions=True)


async def test_sdk_close_failure_preserves_query_and_materialization() -> None:
    """SDK の close が失敗したら、子完了や一時入力の解放を補造しない。"""
    client, query, release = client_with_query()
    query.close.side_effect = RuntimeError("fixture close failure")
    with pytest.raises(RuntimeError, match="fixture close failure"):
        await client.disconnect()
    release.assert_not_awaited()
    assert client._query is query


@pytest.mark.parametrize("cancel", [False, True])
async def test_connect_failure_before_query_closes_the_started_transport(cancel: bool) -> None:
    """Query 未作成でも、接続途中に保持した transport を破棄せず閉じる。"""
    from claude_agent_sdk._internal.transport import Transport

    transport = MagicMock(spec=Transport)
    started = asyncio.Event()

    async def connect() -> None:
        """Process が開始した直後の失敗または取消しを再現する。"""
        started.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("fixture startup failure")

    transport.connect = AsyncMock(side_effect=connect)
    client = DrainingClaudeClient(ClaudeAgentOptions(), transport=transport)
    connecting = asyncio.create_task(client.connect())
    try:
        await started.wait()
        if cancel:
            connecting.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await connecting
        transport.close.assert_awaited_once()
        assert client._transport is None
    finally:
        if not connecting.done():
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.parametrize("has_query", [False, True])
async def test_already_delivered_cancellation_allows_cleanup_before_propagating(
    has_query: bool,
) -> None:
    """元 task が既に取消されていても、終了待機と入力解放を飛ばさない。"""
    from claude_agent_sdk._internal.transport import Transport

    client, query, release = client_with_query()
    transport = MagicMock(spec=Transport)
    client._transport = transport
    if not has_query:
        client._query = None
    started = asyncio.Event()

    async def owner() -> None:
        """Connect/receive を持つ task が、その finally で同じ client を閉じる。"""
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await client.disconnect()

    execution = asyncio.create_task(owner())
    await started.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    release.assert_awaited_once()
    assert client._transport is None
    if has_query:
        query.close.assert_awaited_once()
    else:
        transport.close.assert_awaited_once()


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_pre_query_close_retains_the_original_transport_for_cleanup(
    cancel: bool,
) -> None:
    """終了未確認の transport と入力を保持し、同じ handle の後処理だけを再試行する。"""
    from claude_agent_sdk._internal.transport import Transport

    client, _query, release = client_with_query()
    client._query = None
    transport = MagicMock(spec=Transport)
    client._transport = transport
    entered = asyncio.Event()

    async def close() -> None:
        """最初の cleanup を失敗または取消しで中断する。"""
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("fixture close failure")

    transport.close = AsyncMock(side_effect=close)
    closing = asyncio.create_task(client.disconnect())
    await entered.wait()
    if cancel:
        closing.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await closing
    release.assert_not_awaited()
    assert client._transport is transport
    transport.close.side_effect = None
    await client.disconnect()
    assert transport.close.await_count == 2
    release.assert_awaited_once()
    assert client._transport is None
