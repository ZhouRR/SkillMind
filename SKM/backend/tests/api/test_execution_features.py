"""API 起動時の Run と Effect の配備 policy を一致させる。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_api_shares_http_execution_policy(client: TestClient) -> None:
    """HTTP の独立許可を旧 switch 群への分解で失わない。"""

    features = client.app.state.run_service._execution_features
    assert features is client.app.state.effect_service._execution_features
    assert features.http_writes is True
    assert client.app.state.run_service._deferred_features_enabled is features.deferred
