"""永続原設定の型・checksum・既存作成要求との互換性を外部 service なしで検証する。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

import pytest

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.schedules.domain import ScheduleOccurrenceConflictError
from skillmind.schedules.occurrence import ScheduleOccurrenceSnapshot, utc_time
from tests.schedules.fakes import occurrence_row, schedule_row


def test_snapshot_roundtrip_preserves_original_intent_and_hash() -> None:
    """現在の定義を読まず、保存済みの原要求の fingerprint をそのまま使う。"""

    row = occurrence_row(schedule_row())
    original = deepcopy(row.snapshot_json)
    decoded = ScheduleOccurrenceSnapshot.from_json(original, checksum=row.snapshot_checksum)
    assert decoded.to_json() == original
    assert decoded.checksum == sha256_hex(canonical_json(original))
    assert decoded.intent.fingerprint() == sha256_hex(canonical_json(original["request"]))
    decoded.intent.input_json["value"] = 9
    assert row.snapshot_json == original


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_version", "v2"),
        ("extra", None),
        ("configuration_version", True),
        ("configuration_version", 1.0),
        ("configuration_version", 0),
        ("claim_row_version", 2_147_483_648),
        ("exhausted", 0),
        ("missed", False),
        ("missed", -1),
        ("name", ""),
        ("schedule_id", "invalid"),
        ("occurrence_at", "2035-01-01T12:00:00"),
        ("occurrence_at", "2035-01-01T12:00:01Z"),
        ("occurrence_at", "2035-01-01T12:00:00+00:00"),
        ("idempotency_key", "replacement-key"),
    ],
)
def test_snapshot_rejects_invalid_shape_and_noncanonical_values(field: str, value: Any) -> None:
    """再計算された hash があっても不正な元データを正規化して通さない。"""

    payload = occurrence_row(schedule_row()).snapshot_json
    payload[field] = value
    with pytest.raises(ScheduleOccurrenceConflictError):
        ScheduleOccurrenceSnapshot.from_json(payload, checksum=sha256_hex(canonical_json(payload)))


@pytest.mark.parametrize("value", [True, 1.0, 2])
def test_snapshot_checks_actual_json_not_python_numeric_equality(value: Any) -> None:
    """保存 hash が旧値のままの数値破損を、1 == True に騙されず拒否する。"""

    row = occurrence_row(schedule_row())
    row.snapshot_json["request"]["input"]["value"] = value
    with pytest.raises(ScheduleOccurrenceConflictError):
        ScheduleOccurrenceSnapshot.from_json(row.snapshot_json, checksum=row.snapshot_checksum)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timezone", "not-a-zone"),
        ("max_runs", True),
        ("max_runs", 0),
        ("cron_expression", "*  * * * *"),
        ("run_at", "2035-01-01T12:00:00Z"),
    ],
)
def test_snapshot_rejects_broken_original_definition(field: str, value: Any) -> None:
    """歴史の時計規則を補修して別の発火に変換しない。"""

    payload = occurrence_row(schedule_row()).snapshot_json
    payload["definition"][field] = value
    with pytest.raises(ScheduleOccurrenceConflictError):
        ScheduleOccurrenceSnapshot.from_json(payload)


def test_naive_time_is_not_interpreted_in_host_timezone() -> None:
    """内部 helper も timezone を省略した発火 identity を受け入れない。"""

    with pytest.raises(ValueError, match="timezone"):
        utc_time(datetime(2035, 1, 1))
