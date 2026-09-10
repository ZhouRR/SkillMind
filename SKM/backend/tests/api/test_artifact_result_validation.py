"""Result v2 の保存時 Artifact 核対を読取で偽造・格上げしないことを確認する。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_result_validation_api import ValidationRunService, _install, _validation


def _artifact_validation(kind: str) -> dict[str, Any]:
    """既存 flag と結果形式を保ったまま、新しい保存済み範囲を用意する。"""

    value = _validation(kind)
    value["reference_checks"].update({
        "version": "skillmind.result-reference-checks/v2",
        "artifacts": "RUN_OWNERSHIP_AND_CONTENT",
    })
    value["artifact_refs_valid"] = True
    return value


@pytest.mark.parametrize("kind", ["OUTCOME_ENVELOPE", "STRUCTURED_OUTPUT"])
def test_saved_v2_checks_are_exposed_without_revalidating_or_rewriting(
    client: TestClient, kind: str,
) -> None:
    """現在の Artifact 到達性を推定せず、保存済みの厳密な v2 値だけを返す。"""

    validation = _artifact_validation(kind)
    original = deepcopy(validation)
    response = client.get(_install(client, ValidationRunService(validation, kind=kind)))
    assert response.status_code == 200
    assert response.json()["result"]["validation"] == original
    assert validation == original


@pytest.mark.parametrize("flag", [
    "schema_valid", "evidence_refs_valid", "change_proposal_refs_valid",
    "outcome_envelope_valid", "artifact_refs_valid",
])
@pytest.mark.parametrize("missing", [False, True])
def test_v2_cannot_claim_artifact_guarantee_with_missing_or_false_success_flags(
    client: TestClient, flag: str, missing: bool,
) -> None:
    """一つの保証の成立から、他の未成立/未記録 flag を補完しない。"""

    validation = _artifact_validation("OUTCOME_ENVELOPE")
    if missing:
        del validation[flag]
    else:
        validation[flag] = False
    response = client.get(_install(client, ValidationRunService(validation)))
    assert response.status_code == 503
    assert response.json()["code"] == "run_result_validation_unavailable"


@pytest.mark.parametrize("version,scope,flag,status", [
    ("v1", "NOT_VERIFIED", True, 503), ("v1", "NOT_VERIFIED", False, 200),
    ("v1", "RUN_OWNERSHIP_AND_CONTENT", True, 503),
    ("v2", "NOT_VERIFIED", True, 503), ("v2", "RUN_OWNERSHIP_AND_CONTENT", True, 200),
])
def test_reference_check_version_and_artifact_flag_are_not_interchangeable(
    client: TestClient, version: str, scope: str, flag: bool, status: int,
) -> None:
    """旧 v1 の未検証宣言と v2 の実内容核対を、読取時に同一視しない。"""

    validation = _validation()
    validation["reference_checks"].update({
        "version": f"skillmind.result-reference-checks/{version}", "artifacts": scope,
    })
    validation["artifact_refs_valid"] = flag
    response = client.get(_install(client, ValidationRunService(validation)))
    assert response.status_code == status


def test_legacy_flag_alone_never_synthesizes_a_new_reference_check(client: TestClient) -> None:
    """旧 metadata の真偽値から新 v2 object を推定して作らない。"""

    validation = {"artifact_refs_valid": True}
    response = client.get(_install(client, ValidationRunService(validation)))
    assert response.status_code == 200
    assert response.json()["result"]["validation"] == validation
