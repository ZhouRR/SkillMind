"""Schedule の公開例、Pydantic と Schema を同じ許可 field で検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from skillmind.api.routes.schedules import (
    ScheduleListResponse,
    ScheduleResponse,
    ScheduleStatusRequest,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _load(relative: str) -> dict[str, Any]:
    """検証対象だけを読み、設定 file や外部 reference を解決しない。"""

    value = json.loads((CONTRACTS / relative).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _validator(part: str) -> Draft202012Validator:
    """局所 $defs を共有し、外部ネットワークを使わずに DTO を指定する。"""

    schema = _load("task-schedule/v1.schema.json")
    return Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": f"#/$defs/{part}"},
        format_checker=FormatChecker(),
    )


@pytest.mark.parametrize(
    ("part", "model", "example"),
    [
        ("schedule", ScheduleResponse, "task-schedule"),
        ("page", ScheduleListResponse, "task-schedule-page"),
        ("status_request", ScheduleStatusRequest, "task-schedule-status-request"),
    ],
)
def test_public_schedule_models_match_required_fields_and_examples(
    part: str,
    model: type[ScheduleResponse | ScheduleListResponse | ScheduleStatusRequest],
    example: str,
) -> None:
    """例の成功だけでなく field の追加/削除と required の漂移も検出する。"""

    schema = _load("task-schedule/v1.schema.json")["$defs"][part]
    public = model.model_json_schema()
    assert set(public["properties"]) == set(schema["properties"])
    assert set(public["required"]) == set(schema["required"])
    assert public["additionalProperties"] is False
    example_body = _load(f"examples/{example}.v1.json")
    _validator(part).validate(example_body)
    _validator(part).validate(model.model_validate(example_body).model_dump(mode="json"))


@pytest.mark.parametrize(
    "change",
    [
        {"row_version": True},
        {"row_version": 0},
        {"run_count": -1},
        {"missed_count": 0.5},
        {"max_runs": 0},
        {"last_outcome": "PENDING"},
        {"skill_version_id": "invalid"},
        {"sources": {"documents": 1}},
        {"worker_id": "internal"},
        {"lease_token_hash": "internal"},
    ],
)
def test_schedule_contract_and_response_reject_invalid_or_internal_facts(
    change: dict[str, Any],
) -> None:
    """認領内部情報や曖昧な時刻を公開成功へ昇格させない。"""

    value = _load("examples/task-schedule.v1.json") | change
    assert not _validator("schedule").is_valid(value)
    with pytest.raises(ValidationError):
        ScheduleResponse.model_validate(value)


@pytest.mark.parametrize(
    "change",
    [{"created_at": "2027-02-29T03:00:00Z"}, {"run_at": "2027-01-01T03:00:00"}],
)
def test_schedule_api_enforces_the_declared_aware_datetime_format(change: dict[str, str]) -> None:
    """Schema の format 拡張依存を仮定せず、実 API parser の時刻拒否を検証する。"""

    schema = _load("task-schedule/v1.schema.json")["$defs"]
    assert schema["nullable_instant"]["format"] == "date-time"
    assert schema["schedule"]["properties"]["created_at"]["format"] == "date-time"
    with pytest.raises(ValidationError):
        ScheduleResponse.model_validate(_load("examples/task-schedule.v1.json") | change)


def test_schedule_page_validates_every_record_without_inventing_last_run_relationship() -> None:
    """最後の skip と以前の Run ID は合法な同居で、完全履歴の代わりにはならない。"""

    schedule = _load("examples/task-schedule.v1.json")
    page: dict[str, Any] = {"schedules": [schedule], "total": 101, "limit": 25, "offset": 100}
    _validator("page").validate(page)
    assert ScheduleListResponse.model_validate(page).schedules[0].last_run_id is not None
    invalid = deepcopy(page)
    invalid["schedules"][0].pop("last_outcome")
    assert not _validator("page").is_valid(invalid)
    with pytest.raises(ValidationError):
        ScheduleListResponse.model_validate(invalid)


@pytest.mark.parametrize("version", [None, True, 0, -1, 1.5])
def test_status_request_requires_the_original_strict_version(version: Any) -> None:
    """旧 client の欠版を補わず、人工の現在値確認と別操作にする。"""

    body = {"status": "PAUSED"}
    if version is not None:
        body["expected_row_version"] = version
    assert not _validator("status_request").is_valid(body)
    with pytest.raises(ValidationError):
        ScheduleStatusRequest.model_validate(body)
