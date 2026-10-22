"""MCP 専有の Run 境界と結果不明保持を DB port double で検証する。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from skillmind.agent.mcp_lease import McpDesktopBusyError, McpDesktopLeases
from skillmind.db.models import McpDesktopLease

ENDPOINT = "https://mcp.example.test/mcp"


@asynccontextmanager
async def transaction():
    """本テストでは transaction 内の判定だけを検証する。実 lock 競争は別検証。"""
    yield


def leases(row, previous_status="RUNNING", probe=None):
    """DB 正本の専有行を返す factory を注入する。"""
    session = AsyncMock()
    session.begin = transaction
    session.scalars.return_value = Mock(one=Mock(return_value=row))
    session.get.side_effect = lambda model, _key: (
        row if model is McpDesktopLease else SimpleNamespace(status=previous_status)
    )

    @asynccontextmanager
    async def factory():
        """各呼出しは同じ観測済み行を返す。"""
        yield session

    return McpDesktopLeases(factory, probe)


def probe_returning(value):
    """終了確認 port の応答を固定する。"""
    return SimpleNamespace(ended=AsyncMock(return_value=value))


async def test_probe_confirmation_releases_unconfirmed_operation_for_next_run():
    """原操作の終了回執を確認できた時だけ、終了済み Run の pending を解く。"""
    old_run, pending, run_id = uuid4(), uuid4(), uuid4()
    row = SimpleNamespace(run_id=old_run, pending_effect_id=pending)
    probe = probe_returning(True)
    config = {"server_url": ENDPOINT}
    observed = await leases(row, "FAILED", probe).acquire(
        ENDPOINT, run_id, config=config, credential="token"
    )
    probe.ended.assert_awaited_once_with(
        run_id=old_run, effect_id=pending, config=config, credential="token"
    )
    assert (row.run_id, row.pending_effect_id) == (run_id, None)
    assert observed["pending_operation"] is False


async def test_unconfirmed_probe_keeps_desktop_reserved():
    """照会で終了を確認できない間は従来どおり busy を返す。"""
    old_run, pending = uuid4(), uuid4()
    row = SimpleNamespace(run_id=old_run, pending_effect_id=pending)
    with pytest.raises(McpDesktopBusyError):
        await leases(row, "FAILED", probe_returning(False)).acquire(
            ENDPOINT, uuid4(), config={"server_url": ENDPOINT}, credential="token"
        )
    assert (row.run_id, row.pending_effect_id) == (old_run, pending)


async def test_running_owner_and_missing_credentials_are_not_probed():
    """実行中の別 Run と、照会資格のない呼出しでは原操作を照会しない。"""
    pending = uuid4()
    for status, kwargs in (
        ("RUNNING", {"config": {"server_url": ENDPOINT}, "credential": "token"}),
        ("FAILED", {}),
    ):
        row = SimpleNamespace(run_id=uuid4(), pending_effect_id=pending)
        probe = probe_returning(True)
        with pytest.raises(McpDesktopBusyError):
            await leases(row, status, probe).acquire(ENDPOINT, uuid4(), **kwargs)
        probe.ended.assert_not_awaited()
        assert row.pending_effect_id == pending


async def test_own_effect_retry_is_not_probed():
    """同じ Effect の再 claim は原操作としてそのまま続行する。"""
    run_id, effect_id = uuid4(), uuid4()
    row = SimpleNamespace(run_id=run_id, pending_effect_id=effect_id)
    probe = probe_returning(True)
    await leases(row, "RUNNING", probe).acquire(
        ENDPOINT, run_id, begin_effect=effect_id, config={"server_url": ENDPOINT}, credential="t"
    )
    probe.ended.assert_not_awaited()
    assert row.pending_effect_id == effect_id


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
    observed = await source.acquire(
        "https://mcp.example.test/mcp", run_id, begin_effect=effect_id
    )
    assert observed["run_id"] == str(run_id)
    assert observed["lease_ref"].endswith(":" + str(run_id))
    assert observed["pending_operation"] is True
    assert observed["scope"] == "SKM_ENDPOINT_ONLY"
    assert observed["external_exclusivity"] == "NOT_VERIFIED"
    assert (row.run_id, row.pending_effect_id) == (run_id, effect_id)
    with pytest.raises(McpDesktopBusyError):
        await source.confirm("https://mcp.example.test/mcp", run_id, uuid4())
    assert row.pending_effect_id == effect_id
    await source.confirm("https://mcp.example.test/mcp", run_id, effect_id)
    assert row.pending_effect_id is None and row.run_id == run_id
