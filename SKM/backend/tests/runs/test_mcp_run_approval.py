"""MCP の Run 限定同意を原作成要求へ固定し、手動同意や他 Provider と混同しない。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from skillmind.api.routes.runs import CreateTaskRunRequest
from skillmind.effects.domain import EffectLeaseValidationError
from skillmind.effects.run_approval import run_auto_approval_actor
from skillmind.runs.creation_request import TaskRunIntent
from tests.runs.creation_fakes import creation_command, creation_intent, stored_creation
from tests.runs.effect_authorization_harness import AuthorizationHarness


def test_mcp_consent_is_explicit_and_part_of_creation_identity():
    """同じ開始 checkbox を使っても、保存済みの DB/Git 同意から権限を逆算しない。"""
    base = replace(creation_intent(), auto_approve=True, auto_approve_git=True)
    consent = replace(base, auto_approve_mcp=True)
    assert base.to_json()["request_version"] == "v3"
    assert consent.to_json()["request_version"] == "v4"
    assert consent.to_json()["auto_approve_mcp"] is True
    assert TaskRunIntent.from_json(consent.to_json()) == consent
    assert base.fingerprint() != consent.fingerprint()
    original = stored_creation(creation_command(base))
    assert run_auto_approval_actor(original, "mcp.call/v1", provider="mcp") is None
    current = stored_creation(creation_command(consent))
    assert run_auto_approval_actor(current, "mcp.call/v1", provider="mcp") == consent.actor_id
    assert run_auto_approval_actor(current, "mcp.call/v1", provider="other") is None
    assert run_auto_approval_actor(current, "issue.update/v1", provider="redmine") is None


def test_mcp_consent_without_git_is_valid_and_does_not_grant_git():
    """外部 API は MCP だけの限定同意も表現でき、他の効果を自動で含めない。"""
    intent = replace(creation_intent(), auto_approve=True, auto_approve_mcp=True)
    assert TaskRunIntent.from_json(intent.to_json()) == intent
    assert "auto_approve_git" not in intent.to_json()
    run = stored_creation(creation_command(intent))
    assert run_auto_approval_actor(run, "mcp.call/v1", provider="mcp") == intent.actor_id
    assert run_auto_approval_actor(run, "repository.write/v1", provider="git") is None


@pytest.mark.parametrize("changes", [{"request_hash": "0" * 64}, {"actor": True}])
def test_mcp_approval_requires_intact_original_request(changes):
    """同意 hash や開始者を変更した原要求は承認源にならない。"""
    intent = replace(creation_intent(), auto_approve=True, auto_approve_mcp=True)
    run = stored_creation(creation_command(intent))
    if "request_hash" in changes:
        run.request_hash = changes["request_hash"]
    else:
        run.permission_snapshot_json = {**run.permission_snapshot_json, "actor_id": str(uuid4())}
    with pytest.raises(EffectLeaseValidationError):
        run_auto_approval_actor(run, "mcp.call/v1", provider="mcp")


def test_api_and_domain_require_main_consent_but_no_new_required_field():
    """省略は既存手動動作、MCP 同意だけの指定は許可しない。"""
    body = {"skill_version_id": str(uuid4()), "task_key": "execute", "input": {}, "sources": {}}
    assert CreateTaskRunRequest(**body).auto_approve_mcp is False
    with pytest.raises(ValidationError):
        CreateTaskRunRequest(**body, auto_approve_mcp=True)
    with pytest.raises(ValueError):
        replace(creation_intent(), auto_approve_mcp=True)
    assert CreateTaskRunRequest(**body, auto_approve=True, auto_approve_mcp=True).auto_approve_mcp


@pytest.mark.parametrize("value", [1, "true", None])
def test_mcp_consent_is_strict_boolean(value):
    """文字列や整数を利用者の同意と解釈しない。"""
    body = {"skill_version_id": str(uuid4()), "task_key": "execute", "auto_approve": True}
    with pytest.raises(ValidationError):
        CreateTaskRunRequest(**body, auto_approve_mcp=value)


@pytest.mark.parametrize("mutation", [None, "member", "cancel", "approval_actor"])
async def test_extended_consent_keeps_existing_current_authority_checks(monkeypatch, mutation):
    """既存段階認可を使用し、同意で撤権・取消・承認者検証を迂回しない。"""
    from tests.runs.test_run_start_approval import freeze_consent

    h = AuthorizationHarness()
    freeze_consent(h.run, mcp=True)
    h.approval.source, h.approval.preauthorization_id = "RUN_START", None
    # この fixture の既存 DB effect は同じ開始同意と current actor 検証を通す。
    # MCP Provider 独自 scope/read-back は既存 MCP suite で別途確認する。
    if mutation == "member":
        h.member.status = "DISABLED"
    elif mutation == "cancel":
        h.repository.is_cancellation_requested = AsyncMock(return_value=True)
    elif mutation == "approval_actor":
        h.approval.actor_id = uuid4()
    check = h.repository.authorize_effect_step(
        h.claimed, provider_version=h.execution.provider_version
    )
    if mutation is None:
        await check
    else:
        from skillmind.projects.domain import ProjectNotFoundError

        with pytest.raises((EffectLeaseValidationError, ProjectNotFoundError)):
            await check
