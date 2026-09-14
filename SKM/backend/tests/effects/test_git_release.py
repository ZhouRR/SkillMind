"""Git 単独配備の Provider 境界と、手動承認を省略しないことを検証する。"""

from unittest.mock import AsyncMock, Mock

import pytest

from skillmind.core.settings import Settings
from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.outcomes import effect_failure_record
from skillmind.effects.release import configured_execution_features
from skillmind.effects.wiring import create_effect_provider_registry


def test_git_only_registry_requires_explicit_run_consent_for_automatic_approval():
    """Git switch は DB/SVN/Redmine/子実行を開かない。"""
    features = configured_execution_features(Settings(_env_file=None, git_writes_enabled=True))
    assert features.write_capabilities == frozenset({"repository.write/v1"})
    assert features.effect_enabled("repository.write/v1", "commit", provider="git")
    assert not features.effect_enabled("repository.write/v1", "commit", provider="svn")
    assert not features.capability_enabled("subagent.dispatch/v1")
    definition = resolve_effect_capability("repository.write/v1")
    assert not definition.preauthorizable and not definition.run_auto_approvable
    assert definition.supports_run_approval("git")
    assert not definition.supports_run_approval("svn")
    assert definition.supports_supervision("git") and not definition.supports_supervision("svn")
    registry = create_effect_provider_registry(
        features=features,
        effect_service=AsyncMock(),
        secret_resolver=Mock(),
        git_client=Mock(),
        svn_client=Mock(),
    )
    git = registry.resolve(capability_version="repository.write/v1", provider="git")
    assert git.supervised and git.requires_secret
    with pytest.raises(LookupError):
        registry.resolve(capability_version="repository.write/v1", provider="svn")


def test_git_failure_keeps_unknown_outcome_for_readonly_reconciliation():
    """外部書込の不明結果を通常 FAILED から未送信と推定させない。"""
    record = effect_failure_record(
        capability="repository.write/v1",
        provider="git",
        attempt_no=1,
        code="unavailable",
        retryable=False,
        previous=None,
    )
    assert record["code"] == "effect_result_unknown"
    assert record["cause_code"] == "unavailable"
