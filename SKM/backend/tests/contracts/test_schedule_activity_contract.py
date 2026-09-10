"""在途観察の Schema・例・公開 DTO を揃え、内部認領を API と混同しない。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from skillmind.api.routes.schedules import (
    ScheduleActivityResponse,
    SchedulePendingOccurrenceResponse,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def load(relative: str) -> dict[str, Any]:
    """対象契約だけを読み、設定や外部 schema は読み込まない。"""

    value = json.loads((CONTRACTS / relative).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def validator(part: str = "activity") -> Draft202012Validator:
    """同一 file の定義だけで公開投影を検証する。"""

    schema = load("task-schedule/v1.schema.json")
    return Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": f"#/$defs/{part}"}, format_checker=FormatChecker()
    )


@pytest.mark.parametrize("suffix", ["", "-empty", "-legacy"])
def test_activity_examples_match_the_actual_response(suffix: str) -> None:
    """追跡中の空と歴史 unavailable を同じ成功内容へ畳まない。"""

    body = load(f"examples/task-schedule-activity{suffix}.v1.json")
    validator().validate(body)
    response = ScheduleActivityResponse.model_validate(body)
    validator().validate(response.model_dump(mode="json"))
    assert (response.pending is None) == (body["pending"] is None)


@pytest.mark.parametrize(
    ("part", "model"),
    [
        ("activity", ScheduleActivityResponse),
        ("activity_pending", SchedulePendingOccurrenceResponse),
    ],
)
def test_activity_model_and_schema_have_identical_field_allowlists(
    part: str, model: type[ScheduleActivityResponse | SchedulePendingOccurrenceResponse]
) -> None:
    """将来の ORM field 増加が自動的に公開されない required/extra 境界を守る。"""

    schema = load("task-schedule/v1.schema.json")["$defs"][part]
    declared = model.model_json_schema()
    assert set(declared["properties"]) == set(schema["properties"])
    assert set(declared["required"]) == set(schema["required"])
    assert declared["additionalProperties"] is schema["additionalProperties"] is False
    body = load("examples/task-schedule-activity.v1.json")
    example = body if part == "activity" else body["pending"]
    for field in schema["required"]:
        invalid = deepcopy(example)
        del invalid[field]
        assert not validator(part).is_valid(invalid)
        with pytest.raises(ValidationError):
            model.model_validate(invalid)


@pytest.mark.parametrize(
    "change",
    [
        {"row_version": True},
        {"configuration_version": 0},
        {"automatic_attempt_limit": 0.5},
        {"tracking": "UNKNOWN"},
        {"tracking": "LEGACY_UNAVAILABLE"},
        {"tracking": ["TRACKED"]},
        {"worker_id": "internal"},
        {"snapshot_json": {}},
    ],
)
def test_activity_contract_rejects_malformed_roots(change: dict[str, Any]) -> None:
    """未知・旧形式を追跡済み pending の空投影へ修復しない。"""

    body = load("examples/task-schedule-activity.v1.json") | change
    assert not validator().is_valid(body)
    with pytest.raises(ValidationError):
        ScheduleActivityResponse.model_validate(body)


@pytest.mark.parametrize(
    "change",
    [
        {"occurrence_id": "bad"},
        {"attempt_count": True},
        {"attempt_count": 0},
        {"configuration_version": 0},
        {"lease_token_hash": "private"},
        {"idempotency_key": "private"},
        {"snapshot_json": {}},
        {"run_id": None},
    ],
)
def test_activity_contract_rejects_malformed_or_private_pending(change: dict[str, Any]) -> None:
    """観察に不要な復旧 credential/原要求/架空 Run 成功を返さない。"""

    body = load("examples/task-schedule-activity.v1.json")
    body["pending"].update(change)
    assert not validator().is_valid(body)
    with pytest.raises(ValidationError):
        ScheduleActivityResponse.model_validate(body)


@pytest.mark.parametrize(
    "field", ["checked_at", "occurrence_at", "created_at", "updated_at", "lease_expires_at"]
)
@pytest.mark.parametrize("value", ["2035-01-01T12:00:00", "2035-02-29T12:00:00Z"])
def test_actual_activity_parser_enforces_aware_valid_timestamps(field: str, value: str) -> None:
    """format plugin の有無を隠さず、API の実 datetime parser でも拒否を確認する。"""

    body = load("examples/task-schedule-activity.v1.json")
    part = "activity" if field == "checked_at" else "activity_pending"
    assert (
        load("task-schedule/v1.schema.json")["$defs"][part]["properties"][field]["format"]
        == "date-time"
    )
    target = body if field == "checked_at" else body["pending"]
    target[field] = value
    with pytest.raises(ValidationError):
        ScheduleActivityResponse.model_validate(body)
