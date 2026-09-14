"""Key と browser の形式・派生・保存版を分離し、損傷と撤権を fail closed にする。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from skillmind.auth.domain import (
    derive_api_key_proof,
    derive_request_proof,
    derive_session_csrf,
    generate_api_key_credentials,
    generate_session_credentials,
)
from skillmind.auth.sessions import UnauthorizedSessionError, validate_session_credentials
from skillmind.db.models import AuthSession, User


def test_key_and_browser_are_distinct_credentials() -> None:
    """相互の形式を拒否し、内部 proof を browser CSRF と共有しない。"""
    key, browser = generate_api_key_credentials(), generate_session_credentials()
    assert key.session_token != generate_api_key_credentials().session_token
    assert derive_api_key_proof(key.session_token) == key.csrf_token
    assert derive_request_proof(key.session_token) == key.csrf_token
    assert derive_request_proof(browser.session_token) == browser.csrf_token
    assert key.csrf_token.startswith("keyproof1.")
    with pytest.raises(ValueError):
        derive_api_key_proof(browser.session_token)
    with pytest.raises(ValueError):
        derive_session_csrf(key.session_token)


@pytest.mark.parametrize(
    "damage", ["version", "hash", "proof", "revoked", "disabled", "role", "expiry"]
)
def test_api_key_state_and_hashes_fail_closed(damage: str) -> None:
    """元 key が一致しても持久状態・版・role が不正なら受理しない。"""
    key, now = generate_api_key_credentials(), datetime.now(UTC)
    user = User(id=uuid4(), organization_id=uuid4(), status="ACTIVE", system_role="ADMIN")
    row = AuthSession(
        id=uuid4(),
        user_id=user.id,
        token_hash=key.session_token_hash,
        csrf_token_hash=key.csrf_token_hash,
        credential_version=3,
        system_role_at_login="ADMIN",
        revoked_at=None,
        idle_expires_at=now + timedelta(days=1),
        absolute_expires_at=now + timedelta(days=1),
    )
    validate_session_credentials(row, user, session_token=key.session_token, now=now)
    if damage == "version":
        row.credential_version = 2
    if damage == "hash":
        row.token_hash = generate_api_key_credentials().session_token_hash
    if damage == "proof":
        row.csrf_token_hash = generate_api_key_credentials().csrf_token_hash
    if damage == "revoked":
        row.revoked_at = now
    if damage == "disabled":
        user.status = "DISABLED"
    if damage == "role":
        user.system_role = row.system_role_at_login = "USER"
    if damage == "expiry":
        row.idle_expires_at = now
    with pytest.raises(UnauthorizedSessionError):
        validate_session_credentials(row, user, session_token=key.session_token, now=now)
