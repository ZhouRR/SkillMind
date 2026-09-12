"""実 Worker と同じ registry 組立で、capability と原認可 callback の一致を検証する。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.database_write import DATABASE_WRITE_PROVIDER_VERSION
from skillmind.effects.postgres_write import DatabaseWriteReceipt
from skillmind.effects.release import ExecutionFeatures
from skillmind.effects.wiring import create_effect_provider_registry
from tests.effects.database_fixtures import database_execution


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("database", [False, True])
def test_production_registry_matches_readiness_and_provider_versions(deferred, database):
    """readiness の宣言集合と実実装/version を四つの配備組合せで照合する。"""
    features = ExecutionFeatures(deferred, database)
    registry = create_effect_provider_registry(
        features=features,
        effect_service=MagicMock(),
        secret_resolver=MagicMock(),
        git_client=MagicMock(),
        svn_client=MagicMock(),
    )
    assert registry.write_capabilities == features.write_capabilities
    for (capability, provider), definition in registry.snapshot().items():
        assert (
            definition.provider_version
            == resolve_effect_capability(capability).provider_versions[provider]
        )
        assert definition.requires_secret
        assert definition.supervised == (capability == "database.write/v1")
    if not database:
        with pytest.raises(LookupError):
            registry.resolve(capability_version="database.write/v1", provider="postgres")


async def test_database_factory_binds_original_service_and_resolver(monkeypatch):
    """段階検査は dummy 成功 callback に置き換わらず、原 service/version/resolver に届く。"""
    from skillmind.effects import wiring

    source = AsyncMock()
    source.apply.return_value = DatabaseWriteReceipt(
        None, {"id": "row-1", "status": "RUNNING"}, False
    )
    monkeypatch.setattr(wiring, "PostgresDatabaseWriteSource", lambda: source)
    service, resolver = MagicMock(), MagicMock()
    service.authorize_effect_step = AsyncMock()
    registry = create_effect_provider_registry(
        features=ExecutionFeatures(database_writes=True),
        effect_service=service,
        secret_resolver=resolver,
        git_client=MagicMock(),
        svn_client=MagicMock(),
    )
    execution = database_execution()
    await registry.resolve(
        capability_version="database.write/v1", provider="postgres"
    ).implementation.apply(execution, credential="unit-test-value")
    await source.apply.call_args.kwargs["authorize"]()
    assert service.authorize_effect_step.await_count == 2
    service.authorize_effect_step.assert_awaited_with(
        execution,
        "unit-test-value",
        provider_version=DATABASE_WRITE_PROVIDER_VERSION,
        secret_resolver=resolver,
    )
