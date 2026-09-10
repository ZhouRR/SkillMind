"""Schedule activity の認可、公開白名単、破損 Problem と時刻/型を検証する。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from skillmind.api.problems import PROBLEM_DETAILS_SCHEMA
from skillmind.api.routes.schedules import ScheduleActivityResponse, _activity_response
from skillmind.projects import ProjectNotFoundError, ProjectService, ProjectStatus, StoredProject
from skillmind.schedules.domain import (
    ScheduleActivity,
    ScheduleActivityUnavailableError,
    ScheduleNotFoundError,
    ScheduleTracking,
)
from skillmind.schedules.repository_occurrences import _activity
from skillmind.schedules.service import ScheduleService
from tests.api.fakes import FakeAuthService
from tests.schedules.fakes import NOW, occurrence_row, schedule_row


def application(client: TestClient) -> FastAPI:
    """共有 fixture の FastAPI 型を明示し、別の認証設定を作らない。"""

    return cast(FastAPI, client.app)


def activity_fixture(*, tracking: str = "pending") -> ScheduleActivity:
    """既存 ORM fixture と本物の projector から認領公開値を作る。"""

    schedule = schedule_row()
    pending = occurrence_row(schedule) if tracking == "pending" else None
    schedule.row_version = 2
    schedule.occurrence_protocol = 0 if tracking == "legacy" else 1
    return _activity(schedule, pending, checked_at=NOW)


def activity_url(activity: ScheduleActivity) -> str:
    """呼出元の二つの原 identity を endpoint に固定する。"""

    return f"/api/v1/projects/{activity.project_id}/schedules/{activity.schedule_id}/activity"


@pytest.mark.parametrize("tracking", ["pending", "empty", "legacy"])
def test_activity_api_returns_exact_public_fields_with_no_store(
    client: TestClient,
    tracking: str,
) -> None:
    """未決・追跡済み空・旧形式を区別し、内部 worker/key/hash/入力を出さない。"""

    activity = activity_fixture(tracking=tracking)
    service = MagicMock(spec=ScheduleService)
    service.get_activity = AsyncMock(return_value=activity)
    application(client).state.schedule_service = service
    response = client.get(activity_url(activity))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Request-ID"]
    body = response.json()
    assert body == _activity_response(activity).model_dump(mode="json")
    assert set(body) == {
        "schedule_id",
        "project_id",
        "row_version",
        "configuration_version",
        "tracking",
        "checked_at",
        "automatic_attempt_limit",
        "pending",
    }
    if tracking == "pending":
        assert set(body["pending"]) == {
            "occurrence_id",
            "occurrence_at",
            "configuration_version",
            "created_at",
            "updated_at",
            "attempt_count",
            "lease_expires_at",
        }
    else:
        assert body["pending"] is None
    service.get_activity.assert_awaited_once_with(
        project_id=activity.project_id, schedule_id=activity.schedule_id
    )
    service.run_due_schedules.assert_not_called()


@pytest.mark.parametrize("role", ["ADMIN", "USER"])
@pytest.mark.parametrize("status", list(ProjectStatus))
def test_activity_allows_authorized_archived_project_read_without_csrf_or_creator_checks(
    client: TestClient,
    role: str,
    status: ProjectStatus,
) -> None:
    """現在の読者の ProjectReadActor を用い、原 Worker/作成者資格を再発行しない。"""

    activity = activity_fixture()
    auth = FakeAuthService()
    auth.actor = replace(auth.actor, system_role=role)
    projects = MagicMock(spec=ProjectService)
    projects.get_project = AsyncMock(
        return_value=StoredProject(
            project_id=activity.project_id,
            key="fixture",
            name="Project",
            description="",
            status=status,
            settings={},
            retention_days=90,
            row_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    service = MagicMock(spec=ScheduleService)
    service.get_activity = AsyncMock(return_value=activity)
    application(client).state.auth_service = auth
    application(client).state.project_service = projects
    application(client).state.schedule_service = service
    client.headers.pop("X-CSRF-Token", None)
    client.headers.pop("Origin", None)
    response = client.get(activity_url(activity))
    assert response.status_code == 200
    projects.get_project.assert_awaited_once_with(actor=auth.actor, project_id=activity.project_id)
    service.get_activity.assert_awaited_once()
    service._authorize_creator.assert_not_called()


def test_activity_rejects_an_expired_reader_before_business_lookup(client: TestClient) -> None:
    """匿名/撤回済み会話には存在情報を返さず、共有 401 を返す。"""

    activity = activity_fixture()
    service = MagicMock(spec=ScheduleService)
    application(client).state.auth_service = FakeAuthService(unauthorized=True)
    application(client).state.schedule_service = service
    response = client.get(activity_url(activity))
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert response.headers["Cache-Control"] == "no-store"
    service.get_activity.assert_not_called()


@pytest.mark.parametrize("reason", ["missing", "cross organization", "removed membership"])
def test_activity_project_rejection_does_not_disclose_the_reason(
    client: TestClient,
    reason: str,
) -> None:
    """Project 境界の拒否は在途の有無を照会せず、共通 404 に畳む。"""

    activity = activity_fixture()
    projects = MagicMock(spec=ProjectService)
    projects.get_project = AsyncMock(side_effect=ProjectNotFoundError(reason))
    service = MagicMock(spec=ScheduleService)
    application(client).state.project_service = projects
    application(client).state.schedule_service = service
    response = client.get(activity_url(activity))
    assert response.status_code == 404
    assert response.json()["code"] == "project_not_found"
    assert reason not in response.json()["detail"]
    assert response.headers["Cache-Control"] == "no-store"
    service.get_activity.assert_not_called()


@pytest.mark.parametrize(
    "error", [ScheduleNotFoundError("missing"), ScheduleNotFoundError("foreign schedule")]
)
def test_activity_uses_original_schedule_not_found_problem(
    client: TestClient,
    error: ScheduleNotFoundError,
) -> None:
    """Project 内に無い Schedule は、台帳無しの成功や新しい存在 oracle にしない。"""

    activity = activity_fixture()
    service = MagicMock(spec=ScheduleService)
    service.get_activity = AsyncMock(side_effect=error)
    application(client).state.schedule_service = service
    response = client.get(activity_url(activity))
    assert response.status_code == 404
    assert response.json()["code"] == "schedule_not_found"
    assert response.headers["Cache-Control"] == "no-store"


def test_activity_corruption_is_sanitized_conflict_not_empty_or_cas_failure(
    client: TestClient,
) -> None:
    """snapshot 診断の原文を出さず、通常編集 CAS と異なる安定 code を返す。"""

    activity = activity_fixture()
    service = MagicMock(spec=ScheduleService)
    service.get_activity = AsyncMock(
        side_effect=ScheduleActivityUnavailableError("private-input-marker")
    )
    application(client).state.schedule_service = service
    response = client.get(activity_url(activity))
    assert response.status_code == 409
    assert response.json()["code"] == "schedule_activity_unavailable"
    assert "private-input-marker" not in response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["Cache-Control"] == "no-store"


def test_activity_identity_validation_is_not_a_successful_empty_read(client: TestClient) -> None:
    """無効 UUID を 422 とし、no-store の処理済み Problem を返す。"""

    service = MagicMock(spec=ScheduleService)
    application(client).state.schedule_service = service
    response = client.get("/api/v1/projects/not-a-uuid/schedules/not-a-uuid/activity")
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    service.get_activity.assert_not_called()


@pytest.mark.parametrize("value", [True, 0, 1.0, "1"])
@pytest.mark.parametrize(
    "field", ["row_version", "configuration_version", "automatic_attempt_limit"]
)
def test_activity_response_requires_strict_positive_root_integers(field: str, value: Any) -> None:
    """観察した版/上限を coercion で補正しない。"""

    body = _activity_response(activity_fixture()).model_dump(mode="json")
    body[field] = value
    with pytest.raises(ValidationError):
        ScheduleActivityResponse.model_validate(body)


@pytest.mark.parametrize("value", [True, 0, 1.0, "1"])
@pytest.mark.parametrize("field", ["configuration_version", "attempt_count"])
def test_activity_response_requires_strict_positive_pending_integers(
    field: str, value: Any
) -> None:
    """試行回数を RunAttempt や真偽値の代用品として受けない。"""

    body = _activity_response(activity_fixture()).model_dump(mode="json")
    body["pending"][field] = value
    with pytest.raises(ValidationError):
        ScheduleActivityResponse.model_validate(body)


@pytest.mark.parametrize(
    "field", ["checked_at", "occurrence_at", "created_at", "updated_at", "lease_expires_at"]
)
def test_activity_response_requires_aware_observation_times(field: str) -> None:
    """ブラウザや API host の local timezone を暗黙の観察基準にしない。"""

    body = _activity_response(activity_fixture()).model_dump(mode="json")
    target = body if field == "checked_at" else body["pending"]
    target[field] = "2035-01-01T12:00:00"
    with pytest.raises(ValidationError):
        ScheduleActivityResponse.model_validate(body)


def test_activity_response_forbids_missing_extra_and_legacy_pending_fields() -> None:
    """白名単の全 field を必須にし、旧形式と具体的認領を混在させない。"""

    original = _activity_response(activity_fixture()).model_dump(mode="json")
    for field in original:
        incomplete = original.copy()
        incomplete.pop(field)
        with pytest.raises(ValidationError):
            ScheduleActivityResponse.model_validate(incomplete)
    for field in original["pending"]:
        incomplete = original | {"pending": original["pending"].copy()}
        incomplete["pending"].pop(field)
        with pytest.raises(ValidationError):
            ScheduleActivityResponse.model_validate(incomplete)
    for body in [
        original | {"tracking": ScheduleTracking.LEGACY_UNAVAILABLE.value},
        original | {"tracking": "UNKNOWN"},
        original | {"worker_id": "private"},
        original | {"pending": original["pending"] | {"snapshot_json": {}}},
        original | {"pending": original["pending"] | {"occurrence_id": "invalid"}},
        original | {"pending": original["pending"] | {"configuration_version": 2}},
    ]:
        with pytest.raises(ValidationError):
            ScheduleActivityResponse.model_validate(body)


def test_activity_openapi_declares_actual_problem_media_and_no_store(client: TestClient) -> None:
    """生成 snapshot とは別に、実 route の既知 response metadata を検証する。"""

    operation = application(client).openapi()["paths"][
        "/api/v1/projects/{project_id}/schedules/{schedule_id}/activity"
    ]["get"]
    for status in (200, 401, 404, 409, 422):
        response = operation["responses"][str(status)]
        assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
        if status != 200:
            assert (
                response["content"]["application/problem+json"]["schema"] == PROBLEM_DETAILS_SCHEMA
            )
