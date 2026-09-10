"""失敗分類専用の資格複写を、接続の無い ORM と本番 authorizer で検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.orm import ORMExecuteState, Session, make_transient_to_detached, object_session

from projectmind.auth.domain import generate_session_credentials
from projectmind.auth.service import AuthenticatedActor
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.db.models import AuthSession, User
from projectmind.users.access import authorize_user_access
from projectmind.users.domain import UserAccess, UserAdministrationDeniedError
from projectmind.users.repository import LockedUsers, authorization_failure_snapshot

_NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
_ACTOR_FIELDS = {"id", "organization_id", "system_role", "status"}
_SESSION_FIELDS = {
    "user_id", "token_hash", "csrf_token_hash", "credential_version", "system_role_at_login",
    "revoked_at", "idle_expires_at", "absolute_expires_at",
}


@pytest.fixture
def original() -> tuple[LockedUsers, UserAccess]:
    """元の期限・資格と無関係な管理対象を合成し、実 credential や DB を用いない。"""

    actor = User(
        id=uuid4(), organization_id=uuid4(), system_role="USER", status="ACTIVE",
        email="synthetic@example.test", display_name="Synthetic", password_hash="not-copied",
    )
    credentials = generate_session_credentials()
    current = AuthSession(
        id=uuid4(), user_id=actor.id, token_hash=credentials.session_token_hash,
        csrf_token_hash=credentials.csrf_token_hash, credential_version=2,
        system_role_at_login="USER", revoked_at=None,
        idle_expires_at=_NOW + timedelta(minutes=30),
        absolute_expires_at=_NOW + timedelta(hours=8), created_at=_NOW, last_seen_at=_NOW,
    )
    access = UserAccess(
        AuthenticatedActor(
            actor.id, actor.organization_id, actor.email, actor.display_name, "USER",
        ),
        uuid4(), credentials.session_token, credentials.csrf_token,
    )
    target = User(id=uuid4(), organization_id=actor.organization_id, status="DISABLED")
    other = AuthSession(id=uuid4(), user_id=target.id)
    return LockedUsers(actor, target, current, (current, other)), access


def test_failure_snapshot_copies_only_required_fields_without_orm_state(
    original: tuple[LockedUsers, UserAccess], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """資格の scalar だけを新 transient model へ写し、target/他会話/Session 操作を除外する。"""

    locked, access = original
    add = Mock(side_effect=AssertionError("Failure snapshots must never be added"))
    merge = Mock(side_effect=AssertionError("Failure snapshots must never be merged"))
    monkeypatch.setattr(Session, "add", add)
    monkeypatch.setattr(Session, "merge", merge)
    snapshot = authorization_failure_snapshot(locked)
    assert snapshot.actor is not locked.actor
    assert snapshot.current_session is not locked.current_session
    assert snapshot.target is None and snapshot.sessions == ()
    for copied, source, fields in (
        (snapshot.actor, locked.actor, _ACTOR_FIELDS),
        (snapshot.current_session, locked.current_session, _SESSION_FIELDS),
    ):
        assert set(vars(copied)) - {"_sa_instance_state"} == fields
        assert all(getattr(copied, name) == getattr(source, name) for name in fields)
        assert object_session(copied) is None
    assert inspect(snapshot.actor).transient
    assert inspect(snapshot.current_session).transient
    authorize_user_access(access, snapshot, now=_NOW, admin=False, write=True)
    add.assert_not_called()
    merge.assert_not_called()


@pytest.mark.parametrize("expiry_kind", ["idle", "absolute"])
@pytest.mark.parametrize("write", [False, True])
def test_expired_source_orm_cannot_trigger_sql_during_failure_authorization(
    original: tuple[LockedUsers, UserAccess], expiry_kind: str, write: bool,
) -> None:
    """元 row は expire しても固定期限は変更せず、新 now の再検証だけで期限切れを拒否する。"""

    locked, access = original
    expiry = _NOW + timedelta(minutes=1)
    locked.current_session.idle_expires_at = (
        expiry if expiry_kind == "idle" else expiry + timedelta(hours=1)
    )
    locked.current_session.absolute_expires_at = (
        expiry if expiry_kind == "absolute" else expiry + timedelta(hours=1)
    )
    # bind を持たない Session は ORM state のみを保持し、接続や実 transaction を作らない。
    make_transient_to_detached(locked.actor)
    make_transient_to_detached(locked.current_session)
    with Session() as database:
        database.add_all([locked.actor, locked.current_session])
        snapshot = authorization_failure_snapshot(locked)
        database.expire_all()

        def reject_sql(state: ORMExecuteState) -> None:
            """失敗済み元 Session の遅延 SELECT が一度でも起きれば検証を失敗させる。"""

            del state
            raise AssertionError("Failure authorization must not execute SQL")

        event.listen(database, "do_orm_execute", reject_sql)
        try:
            assert inspect(locked.actor).expired_attributes
            assert inspect(locked.current_session).expired_attributes
            authorize_user_access(access, snapshot, now=_NOW, admin=False, write=write)
            with pytest.raises(UnauthorizedSessionError):
                authorize_user_access(access, snapshot, now=expiry, admin=False, write=write)
            with pytest.raises(UnauthorizedSessionError):
                authorize_user_access(
                    access, snapshot, now=expiry + timedelta(seconds=1), admin=False, write=write,
                )
            assert min(
                snapshot.current_session.idle_expires_at,
                snapshot.current_session.absolute_expires_at,
            ) == expiry
            assert inspect(locked.actor).expired_attributes
            assert inspect(locked.current_session).expired_attributes
            assert object_session(snapshot.actor) is None
            assert object_session(snapshot.current_session) is None
        finally:
            event.remove(database, "do_orm_execute", reject_sql)


@pytest.mark.parametrize("denial", [
    "owner", "organization", "token", "csrf_hash", "credential_version", "role", "revoked",
    "disabled",
])
def test_failure_snapshot_reuses_all_original_credential_refusals(
    original: tuple[LockedUsers, UserAccess], denial: str,
) -> None:
    """時刻だけの独自認証へ分岐せず、現在の共有 authorizer が持つ全基本拒否を維持する。"""

    locked, access = original
    if denial == "owner":
        locked.current_session.user_id = uuid4()
    elif denial == "organization":
        locked.actor.organization_id = uuid4()
    elif denial == "token":
        locked.current_session.token_hash = "sha256:" + "0" * 64
    elif denial == "csrf_hash":
        locked.current_session.csrf_token_hash = "sha256:" + "0" * 64
    elif denial == "credential_version":
        locked.current_session.credential_version = 1
    elif denial == "role":
        locked.current_session.system_role_at_login = "ADMIN"
    elif denial == "revoked":
        locked.current_session.revoked_at = _NOW - timedelta(seconds=1)
    else:
        locked.actor.status = "DISABLED"
    snapshot = authorization_failure_snapshot(locked)
    for values in (locked, snapshot):
        with pytest.raises(UnauthorizedSessionError):
            authorize_user_access(access, values, now=_NOW, admin=False, write=True)


def test_failure_snapshot_does_not_elevate_write_csrf_or_admin_permissions(
    original: tuple[LockedUsers, UserAccess],
) -> None:
    """読取・書込・ADMIN の違いを helper が決めず、元 authorizer の引数と拒否を保つ。"""

    locked, access = original
    snapshot = authorization_failure_snapshot(locked)
    read_access = replace(access, csrf_token="")
    authorize_user_access(read_access, snapshot, now=_NOW, admin=False, write=False)
    with pytest.raises(CsrfRejectedError):
        authorize_user_access(read_access, snapshot, now=_NOW, admin=False, write=True)
    with pytest.raises(UserAdministrationDeniedError):
        authorize_user_access(access, snapshot, now=_NOW, admin=True, write=True)
