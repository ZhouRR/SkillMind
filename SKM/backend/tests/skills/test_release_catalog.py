"""Interpreter に渡す配備 catalog と共通 checksum を、モデル起動なしで検証する。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from skillmind.core.settings import Settings
from skillmind.effects.release import configured_execution_features
from skillmind.skills import wiring
from skillmind.skills.interpreter import CapabilityCatalogSnapshot, load_capability_catalog
from skillmind.skills.wiring import build_skill_interpreter


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("database", [False, True])
@pytest.mark.parametrize("document", [False, True])
def test_interpreter_catalog_respects_deployment_features(
    monkeypatch, enabled: bool, database: bool, document: bool,
) -> None:
    """読み取り/ローカル出力を保持し、後置能力を別 checksum の候補から除外する。"""
    contracts = Path(__file__).resolve().parents[3] / "contracts"
    settings = Settings(  # type: ignore[call-arg]  # BaseSettings の環境読取を止める。
        _env_file=None, contracts_dir=contracts,
        deferred_features_enabled=enabled, database_writes_enabled=database,
    )
    if document:
        monkeypatch.setattr(
            wiring, "configured_execution_features",
            lambda settings: replace(configured_execution_features(settings), document_writes=True),
        )
    interpreter, catalog, identity, _ = build_skill_interpreter(
        settings, environment_fallback={"ANTHROPIC_MODEL": "claude-test"}
    )
    assert interpreter is not None and catalog is not None and identity is not None
    capabilities = {entry.capability for entry in catalog.capabilities}
    assert {
        "database.read/v1", "mcp.read/v1", "workspace.write/v1",
        "document.inspect/v1", "document.list/v1",
    } <= capabilities
    deferred = {
        "subagent.dispatch/v1",
        "issue.update/v1",
        "repository.write/v1",
    }
    assert deferred <= capabilities if enabled else not deferred & capabilities
    assert ("database.write/v1" in capabilities) is database
    assert ("document.write/v1" in capabilities) is document
    assert ("change.propose/v1" in capabilities) is (enabled or database or document)
    original = load_capability_catalog(contracts / "examples/skill-capability-catalog.v1.json")
    assert (catalog.checksum == original.checksum) is (enabled and database and document)
    assert (
        CapabilityCatalogSnapshot.build(
            catalog_version=catalog.catalog_version, capabilities=catalog.capabilities
        ).checksum
        == catalog.checksum
    )
