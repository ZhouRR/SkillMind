"""MCP 専有の Run 境界と結果不明保持を DB port double で検証する。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from skillmind.agent.mcp_lease import McpDesktopBusyError, McpDesktopLeases


@asynccontextmanager
async def transaction():
    """本テストでは transaction 内の判定だけを検証する。実 lock 競争は別検証。"""
    yield


def leases(row, previous_status="RUNNING"):
    """DB 正本の専有行を返す factory を注入する。"""
    session = AsyncMock()
    session.begin = transaction
    session.scalars.return_value = Mock(one=Mock(return_value=row))
    session.get.return_value = SimpleNamespace(status=previous_status)

    @asynccontextmanager
    async def factory():
        """各呼出しは同じ観測済み行を返す。"""
        yield session

    return McpDesktopLeases(factory)


@pytest.mark.parametrize(
    "previous,pending", [("RUNNING", None), ("CANCELLED", uuid4()), ("FAILED", uuid4())]
)
async def test_other_run_or_unknown_operation_blocks_desktop(previous, pending):
    """Run 終了だけでは遠端操作の終了を宣言しない。"""
    row = SimpleNamespace(run_id=uuid4(), pending_effect_id=pending)
    original = row.run_id
    with pytest.raises(McpDesktopBusyError):
        await leases(row, previous).acquire(
            "https://mcp.example.test/mcp", uuid4(), begin_effect=uuid4()
        )
    assert row.run_id == original and row.pending_effect_id == pending


async def test_terminal_confirmed_owner_can_transfer_and_only_original_effect_clears():
    """旧 Run 終了+未確認操作なしの場合だけ専有を移す。"""
    row = SimpleNamespace(run_id=uuid4(), pending_effect_id=None)
    source = leases(row, "SUCCEEDED")
    run_id, effect_id = uuid4(), uuid4()
    await source.acquire("https://mcp.example.test/mcp", run_id, begin_effect=effect_id)
    assert (row.run_id, row.pending_effect_id) == (run_id, effect_id)
    with pytest.raises(McpDesktopBusyError):
        await source.confirm("https://mcp.example.test/mcp", run_id, uuid4())
    assert row.pending_effect_id == effect_id
    await source.confirm("https://mcp.example.test/mcp", run_id, effect_id)
    assert row.pending_effect_id is None and row.run_id == run_id
