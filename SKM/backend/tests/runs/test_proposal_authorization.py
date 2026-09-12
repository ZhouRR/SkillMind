"""Proposal decision が原会話と現在の所属/role を commit 前に復験することを検証する。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.effects.domain import (
    ApprovalDecision,
    ChangeProposalExpiredError,
    DecideProposalCommand,
)
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.runs.repository import RunRepository
from tests.runs.test_interaction_authorization import AuthorizationDatabase


def decision(database):
    """HTTP 開始時の ADMIN flag が古い状況を含む精確判断を作る。"""
    return DecideProposalCommand(
        project_id=database.project.id,
        run_id=database.run.id,
        proposal_id=uuid4(),
        actor_id=database.user.id,
        actor_is_administrator=True,
        decision=ApprovalDecision.APPROVED,
        proposal_version=1,
        proposal_checksum="sha256:" + "a" * 64,
        idempotency_key="proposal:authorized:001",
        reason="Reviewed",
        trace_id=None,
    )


async def test_cached_administrator_flag_is_replaced_by_locked_role(monkeypatch):
    """command と入口 actor の ADMIN 表示では、現在 USER の批准権を拡張できない。"""
    db = AuthorizationDatabase()
    db.access = replace(db.access, actor=replace(db.access.actor, system_role="ADMIN"))
    pending = decision(db)
    store = AsyncMock(return_value=object())
    monkeypatch.setattr(RunRepository, "decide_change_proposal", store)
    await db.service.decide_change_proposal(pending, access=db.access)
    assert store.call_args.args[0].actor_is_administrator is False
    assert pending.actor_is_administrator is True
    assert db.transaction.committed


@pytest.mark.parametrize(
    "failure",
    [
        "revoked",
        "disabled",
        "idle",
        "absolute",
        "csrf",
        "token",
        "removed",
        "missing-member",
        "cross-org",
        "archived",
    ],
)
async def test_revoked_original_authority_cannot_reach_decision_store(monkeypatch, failure):
    """判断の replay/新規を調べる前に、原 credential と現在 Project の失効を検出する。"""
    db = AuthorizationDatabase()
    db.invalidate(failure)
    store = AsyncMock()
    monkeypatch.setattr(RunRepository, "decide_change_proposal", store)
    with pytest.raises(
        (UnauthorizedSessionError, CsrfRejectedError, ProjectNotFoundError, ProjectArchivedError)
    ):
        await db.service.decide_change_proposal(decision(db), access=db.access)
    store.assert_not_awaited()
    assert not db.transaction.committed


@pytest.mark.parametrize("mode", ["first", "replay", "expired"])
async def test_session_expiry_after_decision_wait_rolls_back_outbox_and_expiry(monkeypatch, mode):
    """決定後/flush の待機で期限を超えたら、expiry の commit も含めて取り消す。"""
    db = AuthorizationDatabase()
    previous = list(db.rows)

    async def store(command):
        """DB 効果だけを staging し、最終会話検証で rollback されるか観測する。"""
        db.rows.append(object())
        if mode == "expired":
            raise ChangeProposalExpiredError("expired proposal")
        return object()

    monkeypatch.setattr(RunRepository, "decide_change_proposal", AsyncMock(side_effect=store))
    db.on_flush = lambda: db.invalidate("idle")
    with pytest.raises(UnauthorizedSessionError):
        await db.service.decide_change_proposal(decision(db), access=db.access)
    assert not db.transaction.committed
    assert db.rows == previous
