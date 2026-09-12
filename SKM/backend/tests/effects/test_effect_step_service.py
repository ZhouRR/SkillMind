"""段階認可 service が原 binding/凭据の再解決と明示能力 gate を共有することを検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from skillmind.effects.database_write import (
    DATABASE_WRITE_PROVIDER_VERSION,
)
from skillmind.effects.domain import EffectLeaseValidationError
from skillmind.effects.release import ExecutionFeatures
from skillmind.effects.service import EffectService
from skillmind.runs.repository import RunRepository
from tests.documents.test_document_library_binding import target
from tests.effects.database_fixtures import database_execution


async def test_library_stage_uses_configured_target_without_integration_secret(monkeypatch):
    """共有 repository の現在権限検証を呼び、内部文書庫に架空の凭据を解決しない。"""

    from skillmind.effects import service as module

    library = target()
    claimed = replace(
        database_execution(),
        integration_id=None,
        capability_version="document.write/v1",
        operation="CREATE",
        provider="project-library",
        integration_config={},
        secret_reference_id=None,
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value.__aenter__ = AsyncMock()
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    reached = []

    async def validate(repository, execution, *, provider_version):
        """原配置が transaction 内の本番 repository に届くことを観測する。"""
        assert repository._document_library_target == library
        assert execution == claimed
        reached.append(provider_version)

    monkeypatch.setattr(RunRepository, "authorize_effect_step", validate)
    load = AsyncMock(side_effect=AssertionError("No Integration for the document library"))
    secret = AsyncMock(side_effect=AssertionError("No Integration credential for the library"))
    monkeypatch.setattr(module, "load_bound_run_resource", load)
    monkeypatch.setattr(module, "resolve_binding_secret", secret)
    service = EffectService(
        MagicMock(return_value=session),
        execution_features=ExecutionFeatures(document_writes=True),
        document_library_target=library,
    )
    await service.authorize_effect_step(
        claimed, None, provider_version="project-library-receipt/v2", secret_resolver=MagicMock()
    )
    assert reached == ["project-library-receipt/v2"]
    load.assert_not_awaited()
    secret.assert_not_awaited()
    with pytest.raises(PermissionError):
        await service.authorize_effect_step(
            claimed,
            "unexpected",
            provider_version="project-library-receipt/v2",
            secret_resolver=MagicMock(),
        )


async def test_stage_authorization_is_disabled_by_default() -> None:
    """既存 effect 設定だけで DB Provider の段階実行を自動的に開かない。"""

    factory = MagicMock()
    with pytest.raises(PermissionError, match="not enabled"):
        await EffectService(factory).authorize_effect_step(
            database_execution(),
            "unit-test-value",
            provider_version=DATABASE_WRITE_PROVIDER_VERSION,
            secret_resolver=MagicMock(),
        )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "mutation", [None, "credential", "config", "scope", "revision", "secret", "lease"]
)
async def test_stage_authorization_revalidates_same_binding_and_original_secret(
    monkeypatch, mutation
):
    """共有再検証入口を通り、同じ reference でも実凭据が変われば実行を止める。"""

    from skillmind.effects import service as module

    execution = database_execution()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value.__aenter__ = AsyncMock()
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)
    validate = AsyncMock()
    monkeypatch.setattr(RunRepository, "authorize_effect_step", validate)
    bound = SimpleNamespace(
        scope=deepcopy(execution.integration_scope),
        integration=SimpleNamespace(
            config=deepcopy(execution.integration_config),
            revision=1,
            secret_reference_id=execution.secret_reference_id,
        ),
    )
    load = AsyncMock(return_value=bound)
    resolve = AsyncMock(return_value="unit-test-値")
    monkeypatch.setattr(module, "load_bound_run_resource", load)
    monkeypatch.setattr(module, "resolve_binding_secret", resolve)
    if mutation == "credential":
        resolve.return_value = "changed"
    elif mutation == "config":
        bound.integration.config["host"] = "other.example.test"
    elif mutation == "scope":
        bound.scope["write_columns"].append("example.records.other")
    elif mutation == "revision":
        bound.integration.revision = 2
    elif mutation == "secret":
        bound.integration.secret_reference_id = None
    elif mutation == "lease":
        validate.side_effect = EffectLeaseValidationError("expired")
    service = EffectService(
        factory, execution_features=ExecutionFeatures(database_writes=True)
    )
    arguments = {
        "provider_version": DATABASE_WRITE_PROVIDER_VERSION,
        "secret_resolver": MagicMock(),
    }
    if mutation:
        with pytest.raises(PermissionError):
            await service.authorize_effect_step(execution, "unit-test-値", **arguments)
    else:
        await service.authorize_effect_step(execution, "unit-test-値", **arguments)
        assert load.call_args.kwargs["binding_id"] == execution.binding_id
        assert load.call_args.kwargs["run_id"] == execution.run_id
        assert resolve.call_args.kwargs["integration"] is bound.integration
    if mutation is None or mutation == "lease":
        validate.assert_awaited_once_with(
            execution, provider_version=DATABASE_WRITE_PROVIDER_VERSION
        )
    else:
        validate.assert_not_awaited()
