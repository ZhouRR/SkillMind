"""直接交付の順序・取消・不明結果と通常待機への切替を検証する。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from skillmind.worker.inline_effects import InlineEffectCoordinator
from skillmind.worker.tool_authority import ToolExecutionAuthority
from tests.worker.test_agent_run_executor import _claimed


def fixture():
    """実 executor port の I/O だけを置換し、呼び出し順を確認する。"""
    authority = ToolExecutionAuthority(_claimed())
    proposal, effect = uuid4(), uuid4()
    service = SimpleNamespace(
        begin_inline_effect=AsyncMock(return_value=(proposal, effect)),
        inline_effect_receipt=AsyncMock(return_value={"status": "APPLIED"}),
    )
    executor = SimpleNamespace(execute=AsyncMock(return_value="APPLIED"))
    handler = InlineEffectCoordinator(service, executor, SimpleNamespace(), authority)
    return handler, service, executor, authority, proposal, effect


async def test_no_consent_never_executes():
    """自動同意なしは元の承認待ちへ戻り、Effect executor を呼ばない。"""
    h, s, e, a, p, f = fixture()
    s.begin_inline_effect.return_value = None
    assert await h.invoke({}, "call", str(uuid4())) is None
    e.execute.assert_not_awaited()
    s.inline_effect_receipt.assert_not_awaited()


async def test_returns_only_after_original_execution_and_receipt_commit():
    """保存前の成功を model へ渡さず、同じ原 Effect を一度だけ呼ぶ。"""
    h, s, e, a, p, f = fixture()
    released = asyncio.Event()

    async def execute(*args, **kwargs):
        """外部 apply の完了まで回执読取を遅延する。"""
        await released.wait()

    e.execute.side_effect = execute
    task = asyncio.create_task(h.invoke({}, "call", str(uuid4())))
    await asyncio.sleep(0)
    s.inline_effect_receipt.assert_not_awaited()
    assert not task.done()
    released.set()
    result = await task
    assert result.proposal_id == p and result.receipt == {"status": "APPLIED"}
    e.execute.assert_awaited_once_with(f, inline_parent=a.claimed)


@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("private transport detail")])
async def test_failure_keeps_original_proposal_and_does_not_retry(error):
    """送信後の例外は元提案で延期し、新 ID や暗黙再試行へ変換しない。"""
    h, s, e, a, p, f = fixture()
    e.execute.side_effect = error
    result = await h.invoke({}, "call", str(uuid4()))
    assert result.proposal_id == p and result.receipt is None
    assert e.execute.await_count == 1
    s.inline_effect_receipt.assert_not_awaited()


async def test_cancellation_propagates():
    """Task 取消を普通の失敗回执や成功へ置換しない。"""
    h, s, e, a, p, f = fixture()
    e.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await h.invoke({}, "call", str(uuid4()))
    s.inline_effect_receipt.assert_not_awaited()


async def test_expired_private_scope_cannot_start():
    """別 Attempt/終了済み Worker からの呼出しで新規操作を作らない。"""
    h, s, e, a, p, f = fixture()
    a._active = False
    with pytest.raises(Exception):
        await h.invoke({}, "call", str(uuid4()))
    s.begin_inline_effect.assert_not_awaited()
    e.execute.assert_not_awaited()


@pytest.mark.parametrize("remaining", [0, 300, 329])
async def test_low_attempt_time_selects_deferred_before_creating_effect(remaining):
    """最適化のために親期限を延長せず、元の通常継続へ未送信で戻す。"""
    h, s, e, a, p, f = fixture()
    e.wall_timeout_seconds = 300
    deadline = asyncio.get_running_loop().time() + remaining
    a.deadline = deadline
    assert await h.invoke({}, "call", str(uuid4())) is None
    assert a.deadline == deadline
    s.begin_inline_effect.assert_not_awaited()
    e.execute.assert_not_awaited()


async def test_inline_uses_remaining_attempt_time_without_resetting_it():
    """十分な原余量がある操作だけを実行し、次呼出しのために時刻をリセットしない。"""
    h, s, e, a, p, f = fixture()
    e.wall_timeout_seconds = 300
    deadline = asyncio.get_running_loop().time() + 600
    a.deadline = deadline
    result = await h.invoke({}, "call", str(uuid4()))
    assert result.proposal_id == p
    assert a.deadline == deadline
    e.execute.assert_awaited_once_with(f, inline_parent=a.claimed)


async def test_file_proposal_is_expanded_before_shared_approval_and_changed_file_is_rejected(tmp_path):
    """承認対象には file envelope でなく、hash で固定した原 byte の提案を渡す。"""
    from skillmind.core.hashing import sha256_hex
    from tests.agent.test_workspace_provider import _context
    h, service, executor, authority, proposal, effect = fixture()
    workspace = _context(tmp_path, "workspace.read/v1").workspace
    h._context = SimpleNamespace(workspace=workspace,
        permission_snapshot={"allowed_capabilities": ["workspace.read/v1"]})
    raw = b'{"resource_key":"api","summary":"original","changes":[]}'
    path = workspace.cwd / "proposal.json"
    path.write_bytes(raw)
    args = {"request_file": "workspace/proposal.json", "expected_hash": "sha256:" + sha256_hex(raw)}
    await h.invoke(args, "call", str(uuid4()))
    assert service.begin_inline_effect.await_args.kwargs["arguments"] == {
        "resource_key": "api", "summary": "original", "changes": []}
    path.write_bytes(b'{}')
    with pytest.raises(ValueError, match="changed"):
        await h.invoke(args, "next-call", str(uuid4()))
    assert service.begin_inline_effect.await_count == 1
    assert executor.execute.await_count == 1
