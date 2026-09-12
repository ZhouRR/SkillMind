"""共有段階認可と heartbeat の期限/失権境界を検証する。実 PostgreSQL lock の証明ではない。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from skillmind.effects.database_write import DATABASE_WRITE_PROVIDER_VERSION
from skillmind.effects.domain import EffectLeaseValidationError
from tests.runs.effect_authorization_harness import AuthorizationHarness


async def test_heartbeat_extends_original_active_lease_and_caps_it_at_approval():
    """全段階認可後の新しい時計だけで延長し、元 actor/批准の上限を越えない。"""
    h = AuthorizationHarness()
    original = datetime.now(UTC) + timedelta(seconds=2)
    h.execution.lease_expires_at = original
    h.proposal.expires_at = datetime.now(UTC) + timedelta(seconds=20)
    deadline = await h.repository.heartbeat_effect_execution(
        h.claimed,
        provider_version=DATABASE_WRITE_PROVIDER_VERSION,
        lease_seconds=60,
    )
    assert deadline == h.proposal.expires_at == h.execution.lease_expires_at
    assert deadline > original
    assert h.execution.heartbeat_at <= datetime.now(UTC)
    assert h.locks[:4] == ["Organization", "User", "Project", "ProjectMember"]


@pytest.mark.parametrize("mutation", ["expired", "cancelled", "actor", "approval", "attempt"])
async def test_heartbeat_never_revives_expired_or_revoked_execution(mutation):
    """取消/失効した元 claim の延長を拒否し、現在 row の期限を書き換えない。"""
    h = AuthorizationHarness()
    if mutation == "expired":
        h.execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif mutation == "cancelled":
        h.repository.is_cancellation_requested.return_value = True
    elif mutation == "actor":
        h.actor.status = "DISABLED"
    elif mutation == "approval":
        h.proposal.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        h.execution.attempt_no += 1
    original = h.execution.lease_expires_at
    with pytest.raises(EffectLeaseValidationError):
        await h.repository.heartbeat_effect_execution(
            h.claimed,
            provider_version=DATABASE_WRITE_PROVIDER_VERSION,
            lease_seconds=60,
        )
    assert h.execution.lease_expires_at == original


async def test_wait_after_authorization_cannot_extend_an_expired_lease(monkeypatch):
    """認可後の追加 row 取得待機も寿命を消費し、古い時計で lease を復活させない。"""
    from skillmind.runs import repository_effects

    h = AuthorizationHarness()
    now = datetime.now(UTC)
    h.execution.lease_expires_at = now + timedelta(seconds=1)

    class Clock:
        """DB 待機後の時計だけを進める。sleep や実 DB は使わない。"""

        @classmethod
        def now(cls, tz):
            """共有認可後に更新された現在時刻を返す。"""
            return now

    original_authorize = h.repository.authorize_effect_step

    async def authorize(*args, **kwargs):
        """本番の認可を通過した直後に、追加待機で期限を越えた状況を再現する。"""
        nonlocal now
        authority = await original_authorize(*args, **kwargs)
        now += timedelta(seconds=2)
        return authority

    monkeypatch.setattr(repository_effects, "datetime", Clock)
    monkeypatch.setattr(h.repository, "authorize_effect_step", authorize)
    original = h.execution.lease_expires_at
    with pytest.raises(EffectLeaseValidationError):
        await h.repository.heartbeat_effect_execution(
            h.claimed,
            provider_version=DATABASE_WRITE_PROVIDER_VERSION,
            lease_seconds=60,
        )
    assert h.execution.lease_expires_at == original
