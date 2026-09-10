"""Business API test で共有する認証済み client fixture を提供する。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.fakes import FakeAuthService, FakeProjectAuthorizationService


@pytest.fixture(autouse=True)
def authenticated_business_client(client: TestClient) -> None:
    """公開 business API test に既定 ADMIN session、CSRF、Project access を設定する。"""

    auth = FakeAuthService()
    client.app.state.auth_service = auth
    client.app.state.project_service = FakeProjectAuthorizationService()
    client.headers.update({
        "Origin": "http://testserver",
        "X-CSRF-Token": auth.csrf_token,
    })
