"""未公開の連続実行実験。実 MCP Provider とローカル journal を使い、製品 dispatcher は変更しない。

ここでの承認/所有権 port は fake。SDK、PostgreSQL、Outbox の同時実行保証の証拠ではない。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from skillmind.core.hashing import canonical_json
from skillmind.effects.mcp_call import check_read_back
from skillmind.effects.mcp_provider import McpCallProvider
from skillmind.integrations.mcp_readback import McpReadBackError
from skillmind.integrations.mcp_tools import digest
from tests.agent.test_mcp_tools import encoded, execution


class ReceiptExperiment:
    """試験専用の最大五操作 journal。原操作 ID を再利用し、返却は保存後にだけ許す。"""

    def __init__(self, path, count=3, *, claims=None):
        """架空サービスの承認済み snapshot を固定し、UI/DB 業務語彙を使わない。"""
        if not 1 <= count <= 5:
            raise ValueError("bounded sequence")
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS steps (position INTEGER PRIMARY KEY, "
            "identity TEXT, fingerprint TEXT, attempted INTEGER, receipt TEXT)"
        )
        if claims is None:
            first = self._claim()
            self.claims = [
                replace(first, effect_execution_id=uuid4(), proposal_id=uuid4())
                for _ in range(count)
            ]
        else:
            self.claims = claims
        self.approved = True
        self.authorized = True
        self.cancelled = False
        self.expired = False
        self.remote_status = "DONE"
        self.calls = []
        self.reads = []
        self.lose_response = False
        self.crash_after_receipt = False
        self.provider = McpCallProvider(source=self, leases=AsyncMock(), authorize=self.authorize)
        for position, claim in enumerate(self.claims):
            fingerprint = digest(claim.changes)
            row = self.db.execute(
                "SELECT identity, fingerprint FROM steps WHERE position=?", (position,)
            ).fetchone()
            if row and row != (str(claim.effect_execution_id), fingerprint):
                raise ValueError("original request changed")
            self.db.execute(
                "INSERT OR IGNORE INTO steps VALUES (?, ?, ?, 0, NULL)",
                (position, str(claim.effect_execution_id), fingerprint),
            )
        self.db.commit()

    @staticmethod
    def _claim():
        """通用工具と明示回読条件だけを設定し、続行条件は別にする。"""
        original = execution()
        config = deepcopy(original.integration_config)
        config["tool_catalog"]["tools"] = [
            {
                "name": "perform",
                "description": "Perform one action",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
                "output_schema": None,
            },
            {
                "name": "receipt",
                "description": "Read original receipt",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "state": {"enum": ["DONE", "ERROR", "TIMEOUT"]},
                    },
                    "required": ["id", "state"],
                },
                "read_only_hint": True,
            },
        ]
        config["tool_permissions"] = {"perform": "call", "receipt": "read"}
        return replace(
            original,
            effect_execution_id=uuid4(),
            integration_config=config,
            integration_scope={"resource_uris": [], "tool_names": ["perform", "receipt"]},
            target={"locator": "perform"},
            precondition={"revision": digest(config["tool_catalog"])},
            changes=(
                {
                    "path": "/call",
                    "action": "SET",
                    "value": {
                        "arguments": {"id": "${effect_id}"},
                        "read_back": {
                            "name": "receipt",
                            "arguments": {"id": "${effect_id}"},
                            "checks": [
                                {"path": "/id", "equals": "${effect_id}"},
                                {"path": "/state", "one_of": ["DONE", "ERROR", "TIMEOUT"]},
                            ],
                        },
                    },
                },
            ),
        )

    async def authorize(self, claim, credential):
        """実 Provider が I/O の前後で試験権限を確認する。"""
        if not self.authorized:
            raise PermissionError("revoked")
        if self.cancelled:
            raise asyncio.CancelledError()

    async def call(self, config, credential, name, arguments):
        """送信済み原 ID のみ応答し、ネットワーク I/O を起こさない。"""
        if name == "perform":
            self.calls.append(arguments["id"])
            if self.lose_response:
                # 実 Provider の再 claim を試すため、call 応答直前に原 task を失う。
                self.lose_response = False
                raise asyncio.CancelledError()
        else:
            self.reads.append(arguments["id"])
        return encoded({"id": arguments["id"], "state": self.remote_status})

    async def drive(self):
        """一呼出し中に固定列を進める実験。モデルを再起動せず、未確認なら必ず停止する。"""
        receipts = []
        for position, claim in enumerate(self.claims):
            if self.cancelled:
                raise asyncio.CancelledError()
            if self.expired:
                return "EXPIRED", receipts
            if not self.approved:
                return "WAITING_APPROVAL", receipts
            await self.authorize(claim, "fixture-token")
            attempted, raw = self.db.execute(
                "SELECT attempted, receipt FROM steps WHERE position=?", (position,)
            ).fetchone()
            if raw is None:
                # 実副作用より先に original identity を保存。途中停止後は再照会のみ。
                self.db.execute("UPDATE steps SET attempted=1 WHERE position=?", (position,))
                self.db.commit()
                result = await self.provider.apply(
                    replace(claim, attempt_no=2 if attempted else 1), credential="fixture-token"
                )
                raw = canonical_json(result.after.content["read_back"])
                self.db.execute("UPDATE steps SET receipt=? WHERE position=?", (raw, position))
                self.db.commit()
                if self.crash_after_receipt:
                    self.crash_after_receipt = False
                    raise asyncio.CancelledError()
            receipt = json.loads(raw)
            # 回読確認は実操作の成功と同義でない。保存済み失敗も後続を実行しない。
            try:
                check_read_back({"checks": [{"path": "/state", "equals": "DONE"}]}, receipt)
            except McpReadBackError:
                return "CHECK_FAILED", [*receipts, receipt]
            receipts.append(receipt)
        return "COMPLETE", receipts

    def close(self):
        """試験が作った journal 接続だけを閉じる。"""
        self.db.close()


@pytest.mark.parametrize("count", [1, 3, 5])
async def test_inline_receipt_and_sequence_return_only_committed_facts(tmp_path, count):
    """一回の試験 drive で実 Provider の単步と回読が連続する。SDK 連続性の証明ではない。"""
    pilot = ReceiptExperiment(tmp_path / "journal.db", count)
    try:
        state, receipts = await pilot.drive()
        assert state == "COMPLETE" and len(receipts) == count
        assert pilot.calls == pilot.reads == [str(c.effect_execution_id) for c in pilot.claims]
        assert (
            pilot.db.execute("SELECT count(*) FROM steps WHERE receipt IS NOT NULL").fetchone()[0]
            == count
        )
        # 同じ journal の二回目を原 UI 操作のやり直しにしない。
        assert await pilot.drive() == (state, receipts)
        assert len(pilot.calls) == count
    finally:
        pilot.close()


@pytest.mark.parametrize("state", ["ERROR", "TIMEOUT"])
async def test_confirmed_failure_never_runs_next_step(tmp_path, state):
    """Effect 回読が確認できても、続行条件の失敗で停止する。"""
    pilot = ReceiptExperiment(tmp_path / "journal.db")
    pilot.remote_status = state
    try:
        status, receipts = await pilot.drive()
        assert status == "CHECK_FAILED" and receipts[0]["state"] == state
        assert len(pilot.calls) == 1
    finally:
        pilot.close()


@pytest.mark.parametrize("failure", ["lose_response", "crash_after_receipt"])
async def test_restart_uses_original_identity_without_resending(tmp_path, failure):
    """送信後/保存後の中断を新 connection で再開する。実 PG/Outbox 障害試験ではない。"""
    path = tmp_path / "journal.db"
    pilot = ReceiptExperiment(path)
    claims = pilot.claims
    setattr(pilot, failure, True)
    with pytest.raises(asyncio.CancelledError):
        await pilot.drive()
    calls = list(pilot.calls)
    pilot.close()
    resumed = ReceiptExperiment(path, claims=claims)
    try:
        status, _ = await resumed.drive()
        assert status == "COMPLETE"
        assert calls + resumed.calls == [str(c.effect_execution_id) for c in claims]
        if failure == "lose_response":
            assert resumed.reads[0] == str(claims[0].effect_execution_id)
    finally:
        resumed.close()


@pytest.mark.parametrize("reason", ["approved", "expired", "authorized", "cancelled"])
async def test_no_new_action_without_current_authority_and_budget(tmp_path, reason):
    """手動待機、期限、撤権、取消を成功として続行しない。"""
    pilot = ReceiptExperiment(tmp_path / "journal.db")
    setattr(pilot, reason, reason in {"expired", "cancelled"})
    try:
        if reason == "authorized":
            with pytest.raises(PermissionError):
                await pilot.drive()
        elif reason == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await pilot.drive()
        else:
            status, receipts = await pilot.drive()
            assert status in {"EXPIRED", "WAITING_APPROVAL"} and not receipts
        assert not pilot.calls
    finally:
        pilot.close()
