"""原操作の終了確認 probe が回執の確認時だけ True を返すことを検証する。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.effects import mcp_lease_probe
from skillmind.effects.mcp_lease_probe import McpPendingOperationProbe
from skillmind.effects.mcp_receipt import McpOperationCommand, McpOperationReceipt

ENDPOINT = "https://mcp.example.test/mcp"


@asynccontextmanager
async def _transaction():
    """DB transaction の境界だけを模す。"""
    yield


def _command(run_id, effect_id):
    """元承認から再構築される operation identity を作る。"""
    return McpOperationCommand(effect_id, uuid4(), run_id, uuid4(), "execute_step", "{}")


def _probe(monkeypatch, *, command, original_url=ENDPOINT, receipt=None, lookup_error=None):
    """DB と MCP 通信を差し替えた probe を作る。"""
    session = AsyncMock()
    session.begin = _transaction
    session.get.return_value = SimpleNamespace(project_id=uuid4())

    @asynccontextmanager
    async def factory():
        """各呼出しは同じ session を返す。"""
        yield session

    target = SimpleNamespace(
        command=command, config_json=json.dumps({"server_url": original_url})
    )
    monkeypatch.setattr(
        mcp_lease_probe.RunRepository,
        "load_effect_reconciliation_target",
        AsyncMock(return_value=target),
    )
    lookup = AsyncMock(return_value=receipt, side_effect=lookup_error)
    monkeypatch.setattr(mcp_lease_probe, "lookup_operation", lookup)
    return McpPendingOperationProbe(factory, object()), lookup


async def _ended(probe, run_id, effect_id):
    """現在の接続設定で原操作の終了を問い合わせる。"""
    return await probe.ended(
        run_id=run_id, effect_id=effect_id, config={"server_url": ENDPOINT}, credential="token"
    )


async def test_terminal_receipt_confirms_end(monkeypatch):
    """原 requestId の終了回執がある時だけ True を返す。"""
    run_id, effect_id = uuid4(), uuid4()
    probe, lookup = _probe(
        monkeypatch,
        command=_command(run_id, effect_id),
        receipt=McpOperationReceipt({"status": "completed"}),
    )
    assert await _ended(probe, run_id, effect_id) is True
    lookup.assert_awaited_once()


@pytest.mark.parametrize("failure", ["no_receipt", "transport", "other_endpoint"])
async def test_unconfirmed_probe_never_reports_end(monkeypatch, failure):
    """回執なし・通信失敗・別 endpoint は終了確認として扱わない。"""
    run_id, effect_id = uuid4(), uuid4()
    probe, _ = _probe(
        monkeypatch,
        command=_command(run_id, effect_id),
        original_url="https://other.example.test/mcp" if failure == "other_endpoint" else ENDPOINT,
        lookup_error=RuntimeError("down") if failure == "transport" else None,
    )
    assert await _ended(probe, run_id, effect_id) is False
