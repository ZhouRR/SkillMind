"""Business API test で共有する認証済み client fixture を提供する。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.fakes import FakeAuthService, FakeProjectAuthorizationService


@pytest.fixture(autouse=True)
def authenticated_business_client(client: TestClient) -> None:
    """公開 business API test に既定 ADMIN session、CSRF、Project access を設定する。"""

    auth = FakeAuthService()
    # 拡張機能の既存 API 回帰は明示 full 構成を使う。首版 gate test は false を設定する。
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"deferred_features_enabled": True}
    )
    client.app.state.auth_service = auth
    client.app.state.project_service = FakeProjectAuthorizationService()
    client.headers.update({
        "Origin": "http://testserver",
        "X-CSRF-Token": auth.csrf_token,
    })
