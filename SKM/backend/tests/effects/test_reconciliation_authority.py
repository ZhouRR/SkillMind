"""照会の現在会話/所属を共有 validator で検証する。DB lock は double であり実 PG ではない。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from skillmind.auth.domain import SESSION_CREDENTIAL_VERSION
from skillmind.effects.reconciliation_domain import (
    EffectReconciliationDeniedError,
    EffectReconciliationReference,
    EffectReconciliationTarget,
)
from skillmind.effects.reconciliation_requests import reconciliation_target_checksum
from skillmind.effects.reconciliation_service import EffectReconciliationService
from skillmind.projects.repository import ProjectRepository
from skillmind.runs.repository import RunRepository
from skillmind.users.repository import LockedUsers, UserRepository
from tests.effects.test_database_write import command
from tests.storage.test_object_effect import fixture


def authority(monkeypatch):
    """現在の照会者は元 write 発起人と別。Secret/DB/network は本テストで接続しない。"""
    _, command, _ = fixture()
    now = datetime.now(UTC)
    reference = EffectReconciliationReference(
        uuid4(),
        uuid4(),
        uuid4(),
        command.project_id,
        command.run_id,
        command.effect_id,
    )
    actor = SimpleNamespace(
        id=reference.actor_id,
        organization_id=reference.organization_id,
        status="ACTIVE",
        system_role="USER",
    )
    current = SimpleNamespace(
        id=reference.auth_session_id,
        user_id=actor.id,
        revoked_at=None,
        idle_expires_at=now + timedelta(minutes=5),
        absolute_expires_at=now + timedelta(minutes=10),
        credential_version=SESSION_CREDENTIAL_VERSION,
        system_role_at_login="USER",
    )
    member = SimpleNamespace(project_id=command.project_id, user_id=actor.id, status="ACTIVE")
    project = SimpleNamespace(
        id=command.project_id, organization_id=actor.organization_id, status="ARCHIVED"
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    target = EffectReconciliationTarget(
        uuid4(),
        uuid4(),
        "sha256:" + "1" * 64,
        "project-library",
        "project-library-receipt/v1",
        command,
        "{}",
        None,
    )
    locks = AsyncMock(return_value=LockedUsers(actor, None, current, (current,)))
    membership = AsyncMock(return_value=SimpleNamespace(user=actor, project=project, member=member))
    load = AsyncMock(return_value=target)
    monkeypatch.setattr(UserRepository, "lock_session_reference", locks)
    monkeypatch.setattr(ProjectRepository, "lock_write_access", membership)
    monkeypatch.setattr(RunRepository, "load_effect_reconciliation_target", load)
    documents = MagicMock(namespace=command.namespace)
    documents.lookup = AsyncMock(return_value=None)
    database, resolver = AsyncMock(), MagicMock()
    service = EffectReconciliationService(
        MagicMock(return_value=session),
        secret_resolver=resolver,
        database_reader=database,
        document_reader=documents,
        document_library_target=None,
    )
    return SimpleNamespace(**locals())


async def test_current_project_reader_can_inspect_archived_project_without_old_write_lease(
    monkeypatch,
):
    """現在所属の参照資格だけを使い、古い承認者の会話や write gate/lease を再作成しない。"""
    h = authority(monkeypatch)
    observed = await h.service.observe(h.reference)
    assert observed.status == "NOT_OBSERVED" and observed.kind == "DOCUMENT_OBJECT"
    h.locks.assert_awaited_with(
        organization_id=h.reference.organization_id,
        user_id=h.reference.actor_id,
        session_id=h.reference.auth_session_id,
    )
    h.membership.assert_awaited_with(user=h.actor, project_id=h.reference.project_id)
    assert h.load.await_count == 2
    h.resolver.resolve.assert_not_called()
    h.database.lookup.assert_not_called()
    h.documents.create_once.assert_not_called()


async def test_accepted_target_drift_is_rejected_before_remote_lookup(monkeypatch):
    """受理時の対象と違えば、現在の binding が読めても外部照会を始めない。"""
    h = authority(monkeypatch)
    with pytest.raises(EffectReconciliationDeniedError):
        await h.service.observe(
            h.reference,
            accepted_target_checksum="sha256:" + "0" * 64,
            authorize_request=AsyncMock(),
        )
    h.documents.lookup.assert_not_awaited()


async def test_request_owner_revoked_after_remote_read_withholds_observation(monkeypatch):
    """receipt 取得後にも元核対 owner を検証し、旧 Worker へ観測を渡さない。"""
    h = authority(monkeypatch)
    owner = AsyncMock(side_effect=[None, EffectReconciliationDeniedError("owner expired")])
    with pytest.raises(EffectReconciliationDeniedError):
        await h.service.observe(
            h.reference,
            accepted_target_checksum=reconciliation_target_checksum(h.target),
            authorize_request=owner,
        )
    h.documents.lookup.assert_awaited_once()
    assert owner.await_count == 2


@pytest.mark.parametrize("mutation", ["disabled", "role", "revoked", "idle", "absolute", "member"])
async def test_current_read_authority_is_required_before_loading_original_target(
    monkeypatch, mutation
):
    """元 Effect の所有を読取権に代用せず、失効後に target/Secret/remote を読まない。"""
    h = authority(monkeypatch)
    if mutation == "disabled":
        h.actor.status = "DISABLED"
    elif mutation == "role":
        h.actor.system_role = "ADMIN"
    elif mutation == "revoked":
        h.current.revoked_at = datetime.now(UTC)
    elif mutation == "idle":
        h.current.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif mutation == "absolute":
        h.current.absolute_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        h.member.status = "INACTIVE"
    with pytest.raises(EffectReconciliationDeniedError):
        await h.service.observe(h.reference)
    h.load.assert_not_called()
    h.documents.lookup.assert_not_called()


async def test_session_expiring_during_target_wait_never_starts_external_read(monkeypatch):
    """DB/Artifact 待機を含め返却前に時計を取り直し、入口の会話資格を使い続けない。"""
    h = authority(monkeypatch)

    async def load(*args, **kwargs):
        """対象取得中に原会話が期限切れになった状況を再現する。"""
        h.current.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        return h.target

    h.load.side_effect = load
    with pytest.raises(EffectReconciliationDeniedError):
        await h.service.observe(h.reference)
    h.documents.lookup.assert_not_called()


async def test_revocation_after_network_withholds_read_result(monkeypatch):
    """遠端応答を得ても、返却前に再読した会話が失効していれば観測を公開しない。"""
    h = authority(monkeypatch)

    async def lookup(*args, **kwargs):
        """I/O 完了と最後の参照再検査の間に元会話を失効させる。"""
        h.current.revoked_at = datetime.now(UTC)
        return None

    h.documents.lookup.side_effect = lookup
    with pytest.raises(EffectReconciliationDeniedError):
        await h.service.observe(h.reference)
    assert h.documents.lookup.await_count == 1
    assert h.load.await_count == 1


@pytest.mark.parametrize("rotate", [False, True])
async def test_database_read_reuses_original_binding_and_current_secret_helper(monkeypatch, rotate):
    """Worker の既存 binding/Secret 入口だけを使い、途中の資格変更は観測返却を拒否する。"""
    from skillmind.effects import reconciliation_service as module

    h = authority(monkeypatch)
    original = command(
        effect_id=h.reference.effect_execution_id,
        project_id=h.reference.project_id,
        run_id=h.reference.run_id,
    )
    target = replace(
        h.target,
        provider="postgres",
        provider_version="postgres-receipt/v1",
        command=original,
        secret_reference_id=uuid4(),
    )
    h.load.return_value = target
    bound = SimpleNamespace(
        integration=SimpleNamespace(
            config={},
            secret_reference_id=target.secret_reference_id,
        )
    )
    load_binding = AsyncMock(return_value=bound)
    secrets = AsyncMock(side_effect=["fixture-secret", "rotated" if rotate else "fixture-secret"])
    monkeypatch.setattr(module, "load_bound_run_resource", load_binding)
    monkeypatch.setattr(module, "resolve_binding_secret", secrets)
    h.database.lookup.return_value = None
    if rotate:
        with pytest.raises(EffectReconciliationDeniedError):
            await h.service.observe(h.reference)
    else:
        observed = await h.service.observe(h.reference)
        assert observed.status == "NOT_OBSERVED"
    load_binding.assert_awaited_with(
        h.session,
        project_id=h.reference.project_id,
        run_id=h.reference.run_id,
        binding_id=target.binding_id,
        integration_id=original.integration_id,
        provider="postgres",
        capability="database.write/v1",
    )
    secrets.assert_awaited_with(
        h.session,
        resolver=h.resolver,
        integration=bound.integration,
        required=True,
    )
    assert h.database.lookup.call_args.args[1] == "fixture-secret"
    h.documents.lookup.assert_not_called()
