"""Backend test で共有する環境設定と fixture を定義する。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]

os.environ.setdefault("SKILLMIND_ENVIRONMENT", "test")
os.environ.setdefault("SKILLMIND_CONTRACTS_DIR", str(ROOT / "contracts"))
os.environ.setdefault(
    "SKILLMIND_DATABASE_URL",
    "postgresql+asyncpg://skillmind:skillmind@127.0.0.1:5432/skillmind_test",
)
os.environ.setdefault("SKILLMIND_REDIS_URL", "redis://127.0.0.1:6379/15")


@pytest.fixture
def client() -> TestClient:
    """Application lifespan を含めて検証する TestClient を提供する。"""

    # 環境変数を設定した後に application を import し、Settings cache の初期化順を固定する。
    from skillmind.api import main

    with TestClient(main.create_app()) as test_client:
        yield test_client
