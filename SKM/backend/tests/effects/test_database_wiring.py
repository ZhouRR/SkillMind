"""実 Worker と同じ registry 組立で、capability と原認可 callback の一致を検証する。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, create_autospec

import pytest

from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.release import ExecutionFeatures
from skillmind.effects.service import EffectService
from skillmind.effects.wiring import create_effect_provider_registry


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("database", [False, True])
def test_production_registry_matches_readiness_and_provider_versions(deferred, database, tmp_path):
    """readiness の宣言集合と実実装/version を四つの配備組合せで照合する。"""
    features = ExecutionFeatures(deferred, database)
    registry = create_effect_provider_registry(
        workspace_root=tmp_path,
        features=features,
        effect_service=MagicMock(),
        secret_resolver=MagicMock(),
        git_client=MagicMock(),
        svn_client=MagicMock(),
    )
    assert registry.write_capabilities == features.write_capabilities - {
        "database.write/v1", "issue.update/v1",
    }
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


async def test_database_factory_binds_original_service_and_resolver(tmp_path, monkeypatch):
    """実 apply を通し、全段階で正式な認可引数と同じ認証情報を要求する。"""
    from skillmind.effects import postgres_native
    from skillmind.effects.postgres_native import SQL_VERSION
    from tests.effects.test_native_resource_effects import Connection, sql_execution

    service, resolver = create_autospec(EffectService, instance=True), MagicMock()
    connection = Connection()
    engine = SimpleNamespace(connect=lambda: connection, dispose=AsyncMock())
    monkeypatch.setattr(postgres_native, "create_database_engine", lambda *a, **kw: engine)
    registry = create_effect_provider_registry(
        features=ExecutionFeatures(database_writes=True), workspace_root=tmp_path,
        effect_service=service, secret_resolver=resolver,
        git_client=MagicMock(), svn_client=MagicMock(),
    )
    provider = registry.resolve(
        capability_version="database.execute/v1", provider="postgres",
    ).implementation
    execution = sql_execution()
    result = await provider.apply(execution, credential="fixture-value")
    assert connection.committed and connection.driver.execute.await_count == 1
    assert result.verification["committed"] is True
    assert service.authorize_effect_step.await_args_list == [
        call(execution, "fixture-value", provider_version=SQL_VERSION, secret_resolver=resolver)
    ] * 4
    engine.dispose.assert_awaited_once()
