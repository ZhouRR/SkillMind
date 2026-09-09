"""共通予算の正規化は float、欠測、rate-limit を実消費へ変換しない。"""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from projectmind.runs.budget import (
    BudgetError,
    BudgetPolicy,
    BudgetUsageReport,
    MeteringMode,
    budget_units,
    usd_to_nanos,
)


@pytest.mark.parametrize(
    "value, expected",
    [("0", 0), ("0.000000001", 1), ("1.234567890", 1234567890), (Decimal("0.02"), 20000000)],
)
def test_usd_conversion_preserves_exact_integer_units(value: str | Decimal, expected: int) -> None:
    """Decimal context の丸め設定にも依存せず整数へ変換する。"""

    with localcontext() as context:
        context.prec = 2
        assert usd_to_nanos(value) == expected


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-0.1", "0.0000000001", "1e100", "1e-100", "invalid", 0.1, True]
)
def test_invalid_money_cannot_be_rounded_into_available_credit(value: object) -> None:
    """非有限・負・過剰精度・float をゼロまたは近似値にしない。"""

    with pytest.raises(BudgetError):
        usd_to_nanos(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, True, 0.5, 2**63])
def test_usage_units_are_strict_nonnegative_integers(value: object) -> None:
    """数値らしい値の暗黙 coercion を拒否する。"""

    with pytest.raises(BudgetError):
        budget_units(value)  # type: ignore[arg-type]


def test_policy_round_trip_does_not_enable_missing_cost() -> None:
    """無効な拡張 field や未知 policy を自動互換扱いしない。"""

    policy = BudgetPolicy(20, None, "adapter", "measurement/v1", MeteringMode.CUMULATIVE)
    assert BudgetPolicy.from_json(policy.to_json()) == policy
    for change in ({"version": "run-budget/v2"}, {"free_credit": 20}, {"usd_scale": 100}):
        with pytest.raises(BudgetError):
            BudgetPolicy.from_json(policy.to_json() | change)


def test_missing_usage_and_missing_watermark_are_not_zero_reports() -> None:
    """累積と増分を曖昧な共通カウンターにしない。"""

    with pytest.raises(BudgetError):
        BudgetUsageReport("r", "adapter", "v1", MeteringMode.CUMULATIVE, 0, None)
    with pytest.raises(BudgetError):
        BudgetUsageReport("r", "adapter", "v1", MeteringMode.INCREMENTAL, None, None)
