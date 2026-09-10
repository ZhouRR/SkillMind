"""依存先の後処理が捕えた task 取消しを、次の副作用へ持ち越さない。"""

from __future__ import annotations

import asyncio


async def check_pending_cancellation() -> None:
    """未配送の取消しも checkpoint で受け、所有者の finally より前に伝播する。

    cancelling() だけを見て手動 raise すると、まだ未配送の CancelledError が client の
    cleanup await を後から中断し得る。取消しが無い通常経路では実際に yield しない。
    """

    owner = asyncio.current_task()
    if owner is not None and owner.cancelling():
        await asyncio.sleep(0)
        raise asyncio.CancelledError
