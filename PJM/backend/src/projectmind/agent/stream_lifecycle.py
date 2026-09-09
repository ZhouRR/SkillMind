"""主実行と子実行で共有する async stream の所有者清理を定義する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


async def close_async_stream(stream: AsyncIterator[object]) -> None:
    """aclose を持つ stream の解放を待ち、清理失敗を呼出側へ返す。"""

    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        # 協調取消の上限であり、OS process の停止を証明する deadline ではない。
        await asyncio.wait_for(aclose(), timeout=30)
