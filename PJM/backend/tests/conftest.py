"""Backend test で共有する環境設定と fixture を定義する。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]

os.environ.setdefault("PROJECTMIND_ENVIRONMENT", "test")
os.environ.setdefault("PROJECTMIND_CONTRACTS_DIR", str(ROOT / "contracts"))
os.environ.setdefault(
    "PROJECTMIND_DATABASE_URL",
    "postgresql+asyncpg://projectmind:projectmind@127.0.0.1:5432/projectmind_test",
)
os.environ.setdefault("PROJECTMIND_REDIS_URL", "redis://127.0.0.1:6379/15")


class StubArqPool:
    """API contract test の lifespan だけを満たす外部接続なし queue pool。"""

    async def aclose(self) -> None:
        """In-memory stub は close 対象を持たない。"""


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Application lifespan を含めて検証する TestClient を提供する。"""

    # 環境変数を設定した後に application を import し、Settings cache の初期化順を固定する。
    from projectmind.api import main

    async def create_stub_pool(*args: object, **kwargs: object) -> StubArqPool:
        """API route unit から Redis/ARQ 接続を分離する。"""

        del args, kwargs
        return StubArqPool()

    monkeypatch.setattr(main, "create_pool", create_stub_pool)
    with TestClient(main.create_app()) as test_client:
        yield test_client
