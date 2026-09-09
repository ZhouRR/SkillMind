"""取消 checkpoint が次の副作用と所有者の非同期 cleanup を分けることを検証する。"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from projectmind.core.cancellation import check_pending_cancellation


async def test_no_cancellation_does_not_yield_to_another_callback() -> None:
    """通常の起動境界には追加の event-loop 切替を挿入しない。"""

    observed: list[str] = []
    asyncio.get_running_loop().call_soon(observed.append, "other")
    await check_pending_cancellation()
    assert not observed
    await asyncio.sleep(0)
    assert observed == ["other"]


@pytest.mark.parametrize("already_delivered", [False, True])
async def test_pending_cancellation_is_delivered_before_owner_cleanup(
    already_delivered: bool,
) -> None:
    """未配送/依存先が捕えた取消しの両方で、finally の await を後から中断しない。"""

    observed: list[str] = []

    async def operation() -> None:
        """同期 callback の cancel と、依存先で捕えた cancel を再現する。"""

        owner = asyncio.current_task()
        assert owner is not None
        owner.cancel()
        if already_delivered:
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0)
        try:
            await check_pending_cancellation()
            observed.append("must-not-run")
        finally:
            observed.append("cleanup-start")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            observed.append("cleanup-end")

    task = asyncio.create_task(operation())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed == ["cleanup-start", "cleanup-end"]
