"""実 PostgreSQL で key 発行・再認可・撤銷と組織境界を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from skillmind.auth.api_key_domain import ApiKeyNotFoundError
from skillmind.auth.api_keys import ApiKeyService
from skillmind.auth.domain import derive_api_key_proof, hash_session_secret
from skillmind.auth.service import AuthService
from skillmind.auth.sessions import UnauthorizedSessionError, validate_session_state
from skillmind.db.models import AuthSession, User
from skillmind.users.access import authorize_user_access
from skillmind.users.domain import UserAccess, UserAdministrationDeniedError
from skillmind.users.repository import UserRepository
from sqlalchemy import text
from tests.db.test_real_auth_sessions import seed_auth, session_service
from tests.db.test_real_database_invariants import _session_factory
from tests.db.test_real_database_invariants import migrated_database_url as migrated_database_url


async def test_key_lifecycle_original_credential_and_cross_organization(
    migrated_database_url: str,
) -> None:
    """実 lock/commit で hash 保存、原資格引渡し、別組織拒否と持久失効を確認する。"""
    async with _session_factory(migrated_database_url) as factory:
        user, browser, credentials = await seed_auth(factory)
        access = UserAccess(
            AuthService._actor(user), uuid4(), credentials.session_token, credentials.csrf_token
        )
        service = ApiKeyService(factory)
        with pytest.raises(UserAdministrationDeniedError):
            await service.create(access, "denied")
        async with factory() as session, session.begin():
            stored_user = await session.get(User, user.id)
            stored_session = await session.get(AuthSession, browser.id)
            assert stored_user and stored_session
            stored_user.system_role = stored_session.system_role_at_login = "ADMIN"
        actor = await session_service(factory).authenticate_session(credentials.session_token)
        access = UserAccess(actor, uuid4(), credentials.session_token, credentials.csrf_token)
        created = await service.create(access, " External application ")
        assert created.record.name == "External application"
        assert created.token not in repr(created)
        authenticated, key_id = await service.authenticate(created.token)
        assert authenticated == actor and key_id == created.record.id
        with pytest.raises(UnauthorizedSessionError):
            await session_service(factory).authenticate_session(created.token)
        key_access = UserAccess(actor, uuid4(), created.token, derive_api_key_proof(created.token))
        async with factory() as session, session.begin():
            locked = await UserRepository(session).lock_users(
                access=key_access, target_id=None, include_target_sessions=False
            )
            authorize_user_access(key_access, locked, now=datetime.now(UTC), admin=True, write=True)
            # Worker の原要求 ID 再検証も同じ資格版を許可する。
            validate_session_state(locked.current_session, locked.actor, now=datetime.now(UTC))
            assert locked.current_session.id == key_id
            assert locked.current_session.token_hash == hash_session_secret(created.token)
            assert locked.current_session.credential_version == 3
            raw = await session.scalar(
                text("SELECT row_to_json(api_keys)::text FROM api_keys WHERE id=:id"),
                {"id": key_id},
            )
            assert created.token not in raw
        listed = (await service.list(key_access))[0]
        assert listed.id == key_id and listed.last_used_at is not None
        other_user, _, _other_credentials = await seed_auth(factory)
        # 偽の組織情報で原 credential の owner を上書きしても拒否する。
        forged = UserAccess(
            AuthService._actor(other_user),
            uuid4(),
            created.token,
            derive_api_key_proof(created.token),
        )
        with pytest.raises(UnauthorizedSessionError):
            await service.list(forged)
        with pytest.raises(ApiKeyNotFoundError):
            await service.revoke(access, uuid4())
        revoked = await service.revoke(key_access, key_id)
        assert revoked.revoked_at is not None
        assert (await service.revoke(access, key_id)).revoked_at == revoked.revoked_at
        with pytest.raises(UnauthorizedSessionError):
            await service.authenticate(created.token)
        # 旧 browser は新 key の失効に巻き込まれない。
        assert (
            await session_service(factory).authenticate_session(credentials.session_token) == actor
        )


async def test_revoked_original_key_cannot_commit_after_entry_auth(
    migrated_database_url: str,
) -> None:
    """入口後の持久失効を service の原資格再検証が拒否する。"""
    async with _session_factory(migrated_database_url) as factory:
        user, browser, credentials = await seed_auth(factory)
        async with factory() as session, session.begin():
            u = await session.get(User, user.id)
            s = await session.get(AuthSession, browser.id)
            assert u and s
            u.system_role = s.system_role_at_login = "ADMIN"
        actor = await session_service(factory).authenticate_session(credentials.session_token)
        access = UserAccess(actor, uuid4(), credentials.session_token, credentials.csrf_token)
        service = ApiKeyService(factory)
        created = await service.create(access, "original")
        stale_actor, _ = await service.authenticate(created.token)
        await service.revoke(access, created.record.id)
        stale = UserAccess(stale_actor, uuid4(), created.token, derive_api_key_proof(created.token))
        with pytest.raises(UnauthorizedSessionError):
            await service.create(stale, "must not exist")
        assert len(await service.list(access)) == 1
