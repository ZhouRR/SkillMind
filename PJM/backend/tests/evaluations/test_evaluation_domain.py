"""Evaluation revision の JSON Pointer domain rule を検証する。"""

from __future__ import annotations

import pytest

from projectmind.evaluations.domain import (
    InvalidEvaluationRevisionError,
    resolve_json_pointer,
)


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("/issue/id", "JAF-1"),
        ("/fields/0/value", "before"),
        ("/escaped~1key/~0value", 3),
    ],
)
def test_json_pointer_resolves_existing_result_value(pointer: str, expected: object) -> None:
    """Object、array、escape を通る pointer が Result 原値へ一意に解決される。"""

    result = {
        "issue": {"id": "JAF-1"},
        "fields": [{"value": "before"}],
        "escaped/key": {"~value": 3},
    }

    assert resolve_json_pointer(result, pointer) == expected


@pytest.mark.parametrize(
    "pointer",
    ["", "issue/id", "/missing", "/fields/-", "/fields/01", "/fields/2", "/bad~2key"],
)
def test_json_pointer_rejects_invalid_or_missing_target(pointer: str) -> None:
    """曖昧 index、無効 escape、存在しない field を revision target にしない。"""

    result = {"issue": {"id": "JAF-1"}, "fields": [{"value": "before"}]}

    with pytest.raises(InvalidEvaluationRevisionError):
        resolve_json_pointer(result, pointer)
