"""Run detail の新旧検証範囲、破損拒否と内部 metadata 非公開を検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fakes import FakeRunService
from fastapi import FastAPI
from fastapi.testclient import TestClient

from projectmind.runs.domain import CreatedRun, RunDetail, RunStatus


def _checks(effects: str = "PLATFORM_RECORD_MATCH") -> dict[str, str]:
    """新しい成功候補だけが保存する厳密な五項目を組み立てる。"""

    return {
        "version": "projectmind.result-reference-checks/v1",
        "evidence": "RUN_OWNERSHIP",
        "proposals": "RUN_OWNERSHIP_AND_STATE",
        "effects": effects,
        "artifacts": "NOT_VERIFIED",
    }


def _validation(kind: str = "OUTCOME_ENVELOPE") -> dict[str, Any]:
    """新 protocol の検証済み flag と五項目を同じ意味で組み立てる。"""

    validation: dict[str, Any] = {
        "schema_valid": True, "evidence_refs_valid": True,
        "change_proposal_refs_valid": True,
        "reference_checks": _checks(
            "PLATFORM_RECORD_MATCH" if kind == "OUTCOME_ENVELOPE" else "NOT_APPLICABLE"
        ),
    }
    if kind == "OUTCOME_ENVELOPE":
        validation["outcome_envelope_valid"] = True
    return validation


class ValidationRunService(FakeRunService):
    """他の read model を共有 fake に任せ、保存済み検証 metadata だけを差し替える。"""

    def __init__(self, validation: object, *, kind: str = "OUTCOME_ENVELOPE") -> None:
        """既存 Run と原 metadata を固定し、読取による変更を検出可能にする。"""

        super().__init__()
        self.validation = validation
        self.kind = kind
        self.created_run = CreatedRun(
            run_id=uuid4(), project_id=uuid4(), task_id=uuid4(), status=RunStatus.SUCCEEDED,
            row_version=4, created_at=datetime(2026, 7, 2, 13, 0, tzinfo=UTC),
            idempotent_replay=False,
        )
        self.last_detail: RunDetail | None = None

    async def get_run_detail(self, *, project_id: UUID, run_id: UUID) -> RunDetail:
        """実 route が消費する DTO に原 metadata をそのまま渡す。"""

        detail = await super().get_run_detail(project_id=project_id, run_id=run_id)
        assert detail.result is not None
        self.last_detail = replace(
            detail,
            result=replace(
                detail.result, validation=cast(dict[str, Any], self.validation),
                result_kind=self.kind,
            ),
        )
        return self.last_detail


def _install(client: TestClient, service: ValidationRunService) -> str:
    """Lifespan 付き API fixture に純 fake を接続し、実サービスへ接続させない。"""

    cast(FastAPI, client.app).state.run_service = service
    run = service.created_run
    assert run is not None
    return f"/api/v1/projects/{run.project_id}/runs/{run.run_id}/detail"


@pytest.mark.parametrize("kind,effects", [
    ("OUTCOME_ENVELOPE", "PLATFORM_RECORD_MATCH"),
    ("STRUCTURED_OUTPUT", "NOT_APPLICABLE"),
])
def test_detail_publishes_exact_recorded_checks_without_rewriting_result(
    client: TestClient, kind: str, effects: str,
) -> None:
    """新範囲は原五項目のまま返し、保存 Result/metadata は変更しない。"""

    validation = {
        **_validation(kind),
        "private_diagnostic": {"credential": "synthetic-private-never-public"},
        "task_schema_valid": None, "task_schema_ref": None,
    }
    original = deepcopy(validation)
    service = ValidationRunService(validation, kind=kind)
    response = client.get(_install(client, service))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()["result"]
    assert payload["validation"] == {
        **_validation(kind),
        "task_schema_valid": None, "task_schema_ref": None,
    }
    assert payload["validation"]["reference_checks"]["effects"] == effects
    assert "synthetic-private-never-public" not in response.text
    assert validation == original
    assert service.last_detail is not None and service.last_detail.result is not None
    assert payload["data"] == service.last_detail.result.data


@pytest.mark.parametrize("validation", [{}, {"schema_valid": True}, {
    "schema_ref": "schema://original", "schema_valid": False, "outcome_envelope_valid": False,
    "evidence_refs_valid": False, "evidence_count": 0, "artifact_count": 2,
    "change_proposal_count": 0, "change_proposal_refs_valid": False,
}])
def test_detail_preserves_legacy_absence_and_does_not_infer_checks(
    client: TestClient, validation: dict[str, Any],
) -> None:
    """旧 field と明示 false を保ち、件数や成功状態から新保証を補わない。"""

    response = client.get(_install(client, ValidationRunService(validation)))

    assert response.status_code == 200
    assert response.json()["result"]["validation"] == validation
    assert "reference_checks" not in response.json()["result"]["validation"]


@pytest.mark.parametrize("checks", [None, {}, [], True, "verified", *[
    {key: value for key, value in _checks().items() if key != missing}
    for missing in _checks()
], *[
    {**_checks(), key: value}
    for key, value in (
        ("version", "projectmind.result-reference-checks/v2"),
        ("evidence", "VERIFIED"), ("proposals", True),
        ("effects", "NOT_APPLICABLE"), ("artifacts", "VERIFIED"),
        ("internal_secret", "synthetic-private-never-public"),
    )
]])
def test_detail_rejects_damaged_checks_as_static_no_store_problem(
    client: TestClient, checks: object,
) -> None:
    """欠落/null/未知版/範囲衝突を成功へ裁剪せず、機密を含まない失敗を返す。"""

    validation = {**_validation(), "reference_checks": checks}
    original = deepcopy(validation)
    response = client.get(_install(client, ValidationRunService(validation)))

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "run_result_validation_unavailable"
    assert response.json()["detail"] == (
        "The saved result validation metadata could not be verified."
    )
    assert "synthetic-private-never-public" not in response.text
    assert validation == original


@pytest.mark.parametrize("kind", ["STRUCTURED_OUTPUT", "UNKNOWN"])
def test_detail_rejects_effect_scope_for_wrong_result_kind(
    client: TestClient, kind: str,
) -> None:
    """新保証の意味を結果形式から切り離して表示しない。"""

    response = client.get(_install(client, ValidationRunService(
        _validation(), kind=kind,
    )))

    assert response.status_code == 503


@pytest.mark.parametrize("kind,flag", [
    (kind, flag)
    for kind in ("OUTCOME_ENVELOPE", "STRUCTURED_OUTPUT")
    for flag in ("schema_valid", "evidence_refs_valid", "change_proposal_refs_valid")
] + [("OUTCOME_ENVELOPE", "outcome_envelope_valid")])
@pytest.mark.parametrize("missing", [False, True])
def test_checked_result_rejects_false_or_missing_required_validation_flags(
    client: TestClient, kind: str, flag: str, missing: bool,
) -> None:
    """新範囲を宣言しながら既存検証が不成立/不明の保存結果は成功応答にしない。"""

    validation = _validation(kind)
    if missing:
        del validation[flag]
    else:
        validation[flag] = False
    original = deepcopy(validation)
    response = client.get(_install(client, ValidationRunService(validation, kind=kind)))

    assert response.status_code == 503
    assert response.json()["code"] == "run_result_validation_unavailable"
    assert response.headers["cache-control"] == "no-store"
    assert validation == original


@pytest.mark.parametrize("validation", [None, [], {"schema_valid": "true"}, {
    "evidence_count": True,
}, {"artifact_count": -1}])
def test_detail_does_not_coerce_damaged_existing_validation(
    client: TestClient, validation: object,
) -> None:
    """公開白名单に残す既存値にも型を要求し、文字列や bool を計数へ変換しない。"""

    response = client.get(_install(client, ValidationRunService(validation)))

    assert response.status_code == 503


def test_cross_project_detail_does_not_expose_corrupt_validation(client: TestClient) -> None:
    """所属不一致の 404 を新 metadata の検証より先に維持する。"""

    service = ValidationRunService({"reference_checks": "synthetic-private-never-public"})
    _install(client, service)
    assert service.created_run is not None
    response = client.get(
        f"/api/v1/projects/{uuid4()}/runs/{service.created_run.run_id}/detail"
    )

    assert response.status_code == 404
    assert service.last_detail is None
    assert "synthetic-private-never-public" not in response.text
