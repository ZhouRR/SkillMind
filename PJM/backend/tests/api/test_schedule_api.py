"""TaskSchedule API の契約・作用域・拒否経路を検証する (計画 §22)。

調度は「誰がいつ Run を始めるか」を決める面なので、Project 授権と入力検証がここで緩むと、
Run 側の闸门がすべて正しくても越境した実行が始められてしまう。
"""

from __future__ import annotations

from unittest.mock import MagicMock
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fakes import FakeScheduleService
from fastapi import FastAPI
from fastapi.testclient import TestClient

from projectmind.schedules.domain import ScheduleStatus
from projectmind.schedules.service import PREVIEW_OCCURRENCE_COUNT, ScheduleService

_CRON_DEFINITION = {
    "kind": "CRON",
    "timezone": "Asia/Tokyo",
    "cron_expression": "0 3 * * *",
}


def _app(client: TestClient) -> FastAPI:
    """fixture が提供した FastAPI 型を確認してから fake service を注入する。"""

    assert isinstance(client.app, FastAPI)
    return client.app


@pytest.mark.parametrize("endpoint", ["create", "update", "preview"])
@pytest.mark.parametrize("field", ["run_at", "end_at"])
def test_schedule_writes_reject_timestamps_without_offset(
    client: TestClient, endpoint: str, field: str
) -> None:
    """すべての definition 入口で、process timezone 依存の日時を service 前に拒否する。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake
    definition = {"kind": "ONCE", "timezone": "Asia/Tokyo", "run_at": "2099-01-01T09:00:00+09:00"}
    definition[field] = "2099-01-02T09:00:00"
    base = f"/api/v1/projects/{uuid4()}/schedules"
    if endpoint == "preview":
        response = client.post(f"{base}/preview", json={"definition": definition})
    elif endpoint == "update":
        response = client.put(
            f"{base}/{uuid4()}",
            json={
                "name": "future",
                "definition": definition,
                "expected_row_version": 1,
            },
        )
    else:
        response = client.post(
            base,
            json={
                "name": "future",
                "definition": definition,
                "skill_version_id": str(uuid4()),
                "task_key": "analyze",
            },
        )
    assert response.status_code == 422
    assert fake.created == []
    assert fake.updated == []


def test_list_schedules_returns_project_scoped_records(client: TestClient) -> None:
    """一覧は Project 作用域の schedule だけを返す。"""

    _app(client).state.schedule_service = FakeScheduleService()
    project_id = uuid4()

    response = client.get(f"/api/v1/projects/{project_id}/schedules")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["schedules"][0]["project_id"] == str(project_id)
    assert body["schedules"][0]["cron_expression"] == "0 3 * * *"


@pytest.mark.parametrize("status", list(ScheduleStatus))
def test_schedule_list_passes_filters_and_page_to_service(
    client: TestClient,
    status: ScheduleStatus,
) -> None:
    """帰档を含む単一状態と literal 検索を、全件用 service へ渡す。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake
    project_id = uuid4()
    response = client.get(
        f"/api/v1/projects/{project_id}/schedules",
        params={
            "q": "  nightly_%  ",
            "status": status.value,
            "limit": 7,
            "offset": 14,
        },
    )
    assert response.status_code == 200
    assert fake.listed == [(project_id, 7, 14, "  nightly_%  ", status)]
    assert response.json()["total"] == 1
    assert response.json()["offset"] == 14


@pytest.mark.parametrize(
    "params",
    [
        {"q": "x" * 201},
        {"q": "invalid\x00"},
        {"status": ""},
        {"status": "active"},
        {"status": "UNKNOWN"},
        {"limit": "0"},
        {"limit": "101"},
        {"offset": "-1"},
        [("q", "one"), ("q", "two")],
        [("status", "ACTIVE"), ("status", "PAUSED")],
        [("status", "ACTIVE"), ("status", "ACTIVE")],
    ],
)
def test_schedule_list_rejects_invalid_or_duplicate_filters(
    client: TestClient,
    params: dict[str, str] | list[tuple[str, str]],
) -> None:
    """曖昧な複数値や範囲外ページを黙って最後の値へ変換しない。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake
    response = client.get(f"/api/v1/projects/{uuid4()}/schedules", params=urlencode(params))
    assert response.status_code == 422
    assert fake.listed == []


@pytest.mark.parametrize("query", ["", " " * 200, "x" * 200])
def test_schedule_list_accepts_optional_search_boundaries(client: TestClient, query: str) -> None:
    """空検索と上限長を受理し、省略 status を ACTIVE に読み替えない。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake
    response = client.get(f"/api/v1/projects/{uuid4()}/schedules", params={"q": query})
    assert response.status_code == 200
    assert fake.listed[0][3:] == (query, None)


def test_create_schedule_normalizes_the_definition_before_saving(
    client: TestClient,
) -> None:
    """作成は検証済み定義を service へ渡し、201 と公開 response を返す。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake
    project_id = uuid4()

    response = client.post(
        f"/api/v1/projects/{project_id}/schedules",
        json={
            "name": "  nightly  ",
            "definition": {**_CRON_DEFINITION, "max_runs": 30},
            "skill_version_id": str(uuid4()),
            "task_key": "analyze",
            "input": {"ticket": "T-1"},
            "sources": {"issues": f"integration:{uuid4()}"},
        },
    )

    assert response.status_code == 201
    assert response.json()["project_id"] == str(project_id)
    saved_name, saved_definition = fake.created[0]
    assert saved_name == "  nightly  "  # 名前の trim は service 側の責務。
    assert saved_definition.timezone == "Asia/Tokyo"
    assert saved_definition.max_runs == 30


def test_create_schedule_rejects_an_unknown_timezone(client: TestClient) -> None:
    """未知の timezone は保存前に 422 で弾く。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake

    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules",
        json={
            "name": "nightly",
            "definition": {**_CRON_DEFINITION, "timezone": "Mars/Olympus"},
            "skill_version_id": str(uuid4()),
            "task_key": "analyze",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "schedule_invalid"
    assert fake.created == []


def test_create_schedule_rejects_unsupported_cron_syntax(client: TestClient) -> None:
    """未対応の cron 拡張文法は保存前に 422 で弾く。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake

    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules",
        json={
            "name": "nightly",
            "definition": {**_CRON_DEFINITION, "cron_expression": "@daily"},
            "skill_version_id": str(uuid4()),
            "task_key": "analyze",
        },
    )

    assert response.status_code == 422
    assert fake.created == []


def test_create_schedule_rejects_mixed_kind_fields(client: TestClient) -> None:
    """CRON に単発時刻を混ぜた定義は 422 にする (発火時刻が一意に決まらない)。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake

    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules",
        json={
            "name": "nightly",
            "definition": {**_CRON_DEFINITION, "run_at": "2099-01-01T00:00:00Z"},
            "skill_version_id": str(uuid4()),
            "task_key": "analyze",
        },
    )

    assert response.status_code == 422
    assert fake.created == []


def test_create_schedule_surfaces_task_configuration_failure(client: TestClient) -> None:
    """task/資源選択が通らない設定は 422 にする (保存できて必ず落ちる状態を作らない)。"""

    _app(client).state.schedule_service = FakeScheduleService(invalid=True)

    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules",
        json={
            "name": "nightly",
            "definition": _CRON_DEFINITION,
            "skill_version_id": str(uuid4()),
            "task_key": "analyze",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "schedule_invalid"


def test_preview_returns_upcoming_occurrences_without_saving(client: TestClient) -> None:
    """保存前の発火予告を返し、schedule は作らない。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake

    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules/preview",
        json={"definition": _CRON_DEFINITION},
    )

    assert response.status_code == 200
    # docs/07 §8.3 は保存前に少なくとも三次の提示を求める。
    assert len(response.json()["occurrences"]) >= 3
    assert fake.created == []


def test_preview_returns_five_real_service_candidates(client: TestClient) -> None:
    """mock の三件を公開上限と誤認せず、実 service の五件を契約へ通す。"""

    sessions, skills, runs = MagicMock(), MagicMock(), MagicMock()
    _app(client).state.schedule_service = ScheduleService(
        sessions, skill_service=skills, run_service=runs
    )
    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules/preview", json={"definition": _CRON_DEFINITION}
    )
    assert response.status_code == 200
    assert len(response.json()["occurrences"]) == PREVIEW_OCCURRENCE_COUNT == 5
    sessions.assert_not_called()
    skills.resolve_task_run.assert_not_called()
    runs.create_task_run.assert_not_called()


@pytest.mark.parametrize("value", [True, 1.0, "1"])
@pytest.mark.parametrize("field", ["max_runs", "expected_row_version"])
def test_definition_and_version_do_not_coerce_non_integer_json(
    client: TestClient, value: object, field: str
) -> None:
    """JSON の bool/float/文字列を名額や原版の整数へ変換して受理しない。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake
    definition: dict[str, object] = dict(_CRON_DEFINITION)
    if field == "max_runs":
        definition[field] = value
    response = client.put(
        f"/api/v1/projects/{uuid4()}/schedules/{uuid4()}",
        json={
            "name": "nightly",
            "definition": definition,
            "expected_row_version": value if field == "expected_row_version" else 1,
        },
    )
    assert response.status_code == 422
    assert fake.updated == []


def test_get_schedule_folds_missing_and_forbidden_into_404(client: TestClient) -> None:
    """不存在と越権を同じ 404 に畳む。"""

    _app(client).state.schedule_service = FakeScheduleService(not_found=True)

    response = client.get(f"/api/v1/projects/{uuid4()}/schedules/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["code"] == "schedule_not_found"


def test_update_schedule_requires_the_expected_row_version(client: TestClient) -> None:
    """楽観ロックの不一致は 409 にする。"""

    _app(client).state.schedule_service = FakeScheduleService(conflict=True)

    response = client.put(
        f"/api/v1/projects/{uuid4()}/schedules/{uuid4()}",
        json={
            "name": "nightly",
            "definition": _CRON_DEFINITION,
            "expected_row_version": 1,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "schedule_conflict"


def test_update_schedule_passes_the_row_version_through(client: TestClient) -> None:
    """更新は受信した row_version をそのまま service へ渡す。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake
    schedule_id = uuid4()

    response = client.put(
        f"/api/v1/projects/{uuid4()}/schedules/{schedule_id}",
        json={
            "name": "nightly",
            "definition": _CRON_DEFINITION,
            "expected_row_version": 4,
        },
    )

    assert response.status_code == 200
    assert fake.updated == [(schedule_id, 4)]


def test_change_status_applies_the_requested_transition(client: TestClient) -> None:
    """暂停要求が状態機へそのまま渡ることを確認する。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake
    schedule_id = uuid4()

    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules/{schedule_id}/status",
        json={"status": "PAUSED", "expected_row_version": 4},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "PAUSED"
    assert [item[1].value for item in fake.status_changes] == ["PAUSED"]
    assert fake.status_changes[0][2] == 4


def test_change_status_rejects_a_disallowed_transition(client: TestClient) -> None:
    """状態機が許さない遷移は 409 にする。"""

    _app(client).state.schedule_service = FakeScheduleService(transition_rejected=True)

    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules/{uuid4()}/status",
        json={"status": "ACTIVE", "expected_row_version": 1},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "schedule_transition_invalid"


def test_change_status_reports_a_concurrent_update_as_conflict(client: TestClient) -> None:
    """状態読取後の編集/認領競争を 500 でなく明示的な 409 にする。"""

    fake = FakeScheduleService(conflict=True)
    _app(client).state.schedule_service = fake
    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules/{uuid4()}/status",
        json={"status": "PAUSED", "expected_row_version": 1},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "schedule_conflict"


@pytest.mark.parametrize(
    "body",
    [
        {"status": "PAUSED"},
        {"status": "PAUSED", "expected_row_version": None},
        {"status": "PAUSED", "expected_row_version": True},
        {"status": "PAUSED", "expected_row_version": 1.0},
        {"status": "PAUSED", "expected_row_version": "1"},
        {"status": "PAUSED", "expected_row_version": 0},
        {"status": "PAUSED", "expected_row_version": -1},
        {"status": "PAUSED", "expected_row_version": 1, "extra": True},
    ],
)
def test_status_requires_strict_original_version_without_legacy_fallback(
    client: TestClient,
    body: dict[str, object],
) -> None:
    """原版を送らない旧 browser と不正型を service 呼出し前に拒否する。"""

    fake = FakeScheduleService()
    _app(client).state.schedule_service = fake
    response = client.post(f"/api/v1/projects/{uuid4()}/schedules/{uuid4()}/status", json=body)
    assert response.status_code == 422
    assert fake.status_changes == []


def test_unsafe_schedule_requests_require_csrf(client: TestClient) -> None:
    """CSRF token が一致しない unsafe request は拒否する。"""

    _app(client).state.schedule_service = FakeScheduleService()
    client.headers["X-CSRF-Token"] = "invalid"

    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules",
        json={
            "name": "nightly",
            "definition": _CRON_DEFINITION,
            "skill_version_id": str(uuid4()),
            "task_key": "analyze",
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"


def test_schedule_response_does_not_leak_internal_fields(client: TestClient) -> None:
    """公開 response が許可 field だけを持つことを確認する。"""

    _app(client).state.schedule_service = FakeScheduleService()

    response = client.get(f"/api/v1/projects/{uuid4()}/schedules/{uuid4()}")

    assert response.status_code == 200
    assert set(response.json()) == {
        "schedule_id",
        "project_id",
        "name",
        "kind",
        "status",
        "timezone",
        "cron_expression",
        "run_at",
        "end_at",
        "max_runs",
        "skill_version_id",
        "task_key",
        "input",
        "sources",
        "next_run_at",
        "last_run_at",
        "last_run_id",
        "last_outcome",
        "last_error",
        "run_count",
        "missed_count",
        "created_by",
        "row_version",
        "created_at",
        "updated_at",
    }
