"""HTTP 配備上限を Run 作成まで保持し、他の能力を暗黙許可しない。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import Run
from skillmind.effects.release import ExecutionFeatures
from skillmind.runs.domain import TaskSourceSelectionError
from skillmind.runs.service import RunService
from tests.runs.creation_authorization_harness import CreationAuthorizationHarness


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_http_run_creation_preserves_shared_policy_and_write_binding(enabled: bool) -> None:
    """実作成経路で HTTP を独立許可し、write は観測と提案に分けて凍結する。"""

    db = CreationAuthorizationHarness()
    features = ExecutionFeatures(http_writes=enabled)
    db.service = RunService(db.session_factory, execution_features=features)
    assert db.service._execution_features is features
    assert db.service._deferred_features_enabled is False
    assert db.service._scheduling_enabled is False
    manifest = deepcopy(db.resolved.skill_snapshot["manifest"])
    capabilities = ("http.read/v1", "http.write/v1")
    manifest["tools"] = [{"capability": cap, "required": True} for cap in capabilities]
    blueprint = manifest["capability_blueprint"]
    blueprint["resource_requirements"] = [{
        "key": "api", "kind": "other", "required": True, "access": "write",
        "capabilities": list(capabilities),
    }]
    blueprint["effect_intents"] = [{
        "key": "update", "resource_key": "api", "mode": "apply",
        "operation": "PATCH", "risk": "medium", "approval_mode": "ask",
    }]
    checksum = f"sha256:{sha256_hex(canonical_json(manifest))}"
    db.resolved = replace(
        db.resolved, allowed_capabilities=capabilities, manifest_checksum=checksum,
        skill_snapshot={**db.resolved.skill_snapshot, "manifest": manifest,
                        "manifest_checksum": checksum},
    )
    db.manifest.manifest_json = manifest
    db.manifest.checksum = checksum
    db.integration.kind = "other"
    db.integration.provider = "http"
    db.integration.capabilities_json = list(capabilities)
    db.integration.scope_json = {}
    db.sources = {"api": f"integration:{db.integration.id}"}
    if not enabled:
        with pytest.raises(TaskSourceSelectionError, match="disabled execution features"):
            await db.call()
        assert db.committed == []
        db.session.add_all.assert_not_called()
    else:
        await db.call()
        run = next(row for row in db.committed if isinstance(row, Run))
        assert set(run.permission_snapshot_json["allowed_capabilities"]) == {
            "http.read/v1", "http.write/v1", "change.propose/v1", "interaction.request/v1",
        }
        source = run.selected_sources_json["api"]
        assert source["capability"] == "http.read/v1"
        assert source["binding_capability"] == "http.write/v1"
        assert source["access"] == "write"
        assert features.write_capabilities == frozenset({"http.write/v1"})
