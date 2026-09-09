"""公開 schedule DTO が壊れた型を成功 response として補正しないことを検証する。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from projectmind.api.routes.schedules import (
    ScheduleListResponse,
    ScheduleResponse,
    _schedule_response,
)
from projectmind.schedules.repository import _to_record
from tests.schedules.fakes import schedule_row


def schedule_payload() -> dict[str, Any]:
    """正式 projector が返す既存 field だけの response を基準にする。"""

    return _schedule_response(_to_record(schedule_row())).model_dump(mode="json")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("extra", True),
        ("schedule_id", "invalid"),
        ("project_id", "invalid"),
        ("skill_version_id", "invalid"),
        ("created_by", "invalid"),
        ("run_count", -1),
        ("run_count", True),
        ("run_count", 1.0),
        ("missed_count", -1),
        ("missed_count", "1"),
        ("row_version", 0),
        ("row_version", False),
        ("row_version", 1.0),
        ("max_runs", 0),
        ("max_runs", True),
        ("status", "UNKNOWN"),
        ("kind", "WATCH"),
        ("last_outcome", "UNKNOWN"),
        ("sources", {"x": 1}),
        ("name", ""),
        ("timezone", ""),
        ("task_key", ""),
    ],
)
def test_schedule_response_rejects_invalid_public_values(field: str, value: Any) -> None:
    """余分な内部情報、偽 identity、数値 coercion を response へ通さない。"""

    payload = schedule_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        ScheduleResponse.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "run_at",
        "end_at",
        "next_run_at",
        "last_run_at",
        "created_at",
        "updated_at",
    ],
)
def test_schedule_response_requires_explicit_offset_for_every_timestamp(field: str) -> None:
    """nullable な歴史時刻も、存在するときは timezone 不明の値を受理しない。"""

    payload = schedule_payload()
    payload[field] = "2035-01-01T12:00:00"
    with pytest.raises(ValidationError):
        ScheduleResponse.model_validate(payload)


def test_schedule_response_keeps_independent_historical_summary_fields() -> None:
    """直近の見送りと以前の Run 参照は共存し、勝手な整合関係を要求しない。"""

    payload = schedule_payload()
    payload.update(last_outcome="SKIPPED_OVERLAP", last_run_id=str(uuid4()), last_run_at=None)
    assert ScheduleResponse.model_validate(payload).last_outcome == "SKIPPED_OVERLAP"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("extra", None),
        ("total", -1),
        ("total", True),
        ("limit", 0),
        ("limit", 101),
        ("offset", -1),
        ("offset", 1.0),
    ],
)
def test_schedule_page_rejects_invalid_pagination_shape(field: str, value: Any) -> None:
    """total/page を client が安全に解釈できる厳格な整数へ固定する。"""

    payload: dict[str, Any] = {
        "schedules": [schedule_payload()],
        "total": 1,
        "limit": 20,
        "offset": 0,
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        ScheduleListResponse.model_validate(payload)


def test_schedule_models_declare_extra_rejection_and_required_original_version() -> None:
    """実 validator と OpenAPI の元 Schema が同じ必須・型境界を宣言する。"""

    from projectmind.api.routes.schedules import ScheduleStatusRequest

    status = ScheduleStatusRequest.model_json_schema()
    assert set(status["required"]) == {"status", "expected_row_version"}
    assert status["properties"]["expected_row_version"]["minimum"] == 1
    assert status["additionalProperties"] is False
    assert ScheduleResponse.model_json_schema()["additionalProperties"] is False
    assert ScheduleListResponse.model_json_schema()["additionalProperties"] is False


def test_schedule_page_enforces_public_item_limit() -> None:
    """公開一覧が最大ページ長を超えて返る状態を契約違反として検出する。"""

    with pytest.raises(ValidationError):
        ScheduleListResponse.model_validate(
            {
                "schedules": [schedule_payload()] * 101,
                "total": 101,
                "limit": 100,
                "offset": 0,
            }
        )
