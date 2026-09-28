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
    assert registry.write_capabilities == features.write_capabilities - {"database.write/v1", "issue.update/v1"}
    for (capability, provider), definition in registry.snapshot().items():
        assert (
            definition.provider_version
            == resolve_effect_capability(capability).provider_versions[provider]
        )
        assert definition.requires_secret
        assert definition.supervised == (capability == "database.execute/v1" or provider == "git")
    if not database:
        with pytest.raises(LookupError):
            registry.resolve(capability_version="database.execute/v1", provider="postgres")


async def test_database_factory_binds_original_service_and_resolver():
    """原生 SQL client の段階認可も共有 service/version/resolver を通す。"""
    from skillmind.effects.postgres_native import SQL_VERSION
    from tests.effects.test_native_resource_effects import sql_execution
    service, resolver = MagicMock(), MagicMock()
    service.authorize_effect_step = AsyncMock()
    registry = create_effect_provider_registry(features=ExecutionFeatures(database_writes=True),
        effect_service=service, secret_resolver=resolver, git_client=MagicMock(), svn_client=MagicMock())
    provider = registry.resolve(capability_version="database.execute/v1", provider="postgres").implementation
    execution = sql_execution()
    await provider._authorize(execution, "fixture-value")
    service.authorize_effect_step.assert_awaited_once_with(execution, "fixture-value",
        provider_version=SQL_VERSION, secret_resolver=resolver)
