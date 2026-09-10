"""cron 式の解析と timezone 上での次回発火算出を検証する (計画 §22 D2/D3)。

外部依存を入れずに実装している以上、文法の受理範囲と DST 二種の曖昧点はここで固定する。
特に「春の存在しない時刻は飛ばす」「秋の重複する時刻は一回だけ」は、抜けても平常時のテストが
全部通ってしまい、年に二回だけ壊れる類の欠陥になる。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from skillmind.schedules.cron import (
    CronExpressionError,
    next_occurrence,
    normalize_timezone,
    parse_cron,
    upcoming_occurrences,
)


def _utc(text: str) -> datetime:
    """`YYYY-MM-DD HH:MM` を UTC の aware datetime へ変換する。"""

    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


def test_every_field_form_expands_to_the_expected_set() -> None:
    """`*`・単一値・範囲・刻み・列挙がそれぞれ想定どおり展開されることを確認する。"""

    expression = parse_cron("0,30 9-17/4 * * *")

    assert expression.minutes == frozenset({0, 30})
    assert expression.hours == frozenset({9, 13, 17})
    assert expression.days_of_month == frozenset(range(1, 32))
    assert expression.months == frozenset(range(1, 13))
    assert expression.days_of_week == frozenset(range(0, 7))


def test_sunday_is_accepted_as_both_zero_and_seven() -> None:
    """cron の慣習どおり 0 と 7 が同じ日曜へ正規化されることを確認する。"""

    assert parse_cron("0 0 * * 7").days_of_week == frozenset({0})
    assert parse_cron("0 0 * * 0").days_of_week == frozenset({0})


@pytest.mark.parametrize(
    "expression",
    [
        "0 0 * *",
        "0 0 * * * *",
        "60 0 * * *",
        "0 24 * * *",
        "0 0 0 * *",
        "0 0 * 13 *",
        "0 0 * * 8",
        "0 0 L * *",
        "@daily",
        "0 0 5-1 * *",
        "0 0 * * */0",
        "a 0 * * *",
    ],
)
def test_unsupported_or_out_of_range_syntax_is_rejected(expression: str) -> None:
    """未対応の拡張文法と範囲外の値が保存前に弾かれることを確認する。

    黙って別の意味に解釈すると、利用者が書いたつもりの時刻と実際の発火が食い違ったまま運用が続く。
    """

    with pytest.raises(CronExpressionError):
        parse_cron(expression)


def test_day_of_month_and_day_of_week_use_the_posix_or_rule() -> None:
    """日と曜日が双方 `*` 以外なら OR で一致する POSIX 特例を確認する。"""

    expression = parse_cron("0 0 13 * 5")
    # 2026-02-13 は金曜。両方に一致する日。
    assert next_occurrence(expression, after=_utc("2026-02-12 00:00"), timezone="UTC") == _utc(
        "2026-02-13 00:00"
    )
    # 2026-03-06 は金曜だが 13 日ではない。OR なのでここでも発火する。
    assert next_occurrence(expression, after=_utc("2026-03-02 00:00"), timezone="UTC") == _utc(
        "2026-03-06 00:00"
    )


def test_star_day_of_month_keeps_the_weekday_restriction() -> None:
    """片方が `*` のときは AND なので、曜日指定だけが効くことを確認する。"""

    expression = parse_cron("0 0 * * 1")
    occurrences = upcoming_occurrences(
        expression, after=_utc("2026-07-26 00:00"), timezone="UTC", count=3
    )

    assert occurrences == [
        _utc("2026-07-27 00:00"),
        _utc("2026-08-03 00:00"),
        _utc("2026-08-10 00:00"),
    ]


def test_next_occurrence_is_strictly_after_the_boundary() -> None:
    """境界時刻ちょうどは「次」に含めないことを確認する (同じ発火を二度掴まない)。"""

    expression = parse_cron("0 * * * *")

    assert next_occurrence(expression, after=_utc("2026-07-26 09:00"), timezone="UTC") == _utc(
        "2026-07-26 10:00"
    )


def test_local_timezone_is_converted_back_to_utc() -> None:
    """現地時刻で書いた式が UTC へ正しく変換されることを確認する。"""

    expression = parse_cron("30 9 * * *")

    # Asia/Tokyo は UTC+9 固定。現地 09:30 は前日 00:30 UTC。
    assert next_occurrence(
        expression, after=_utc("2026-07-26 00:00"), timezone="Asia/Tokyo"
    ) == _utc("2026-07-26 00:30")


def test_spring_forward_gap_skips_the_nonexistent_local_time() -> None:
    """春の跳ばされた時刻はその回を飛ばす (§22 D3)。

    近傍の時刻へ繰り上げると「毎日 02:30」がその日だけ不定の時刻になる。
    """

    expression = parse_cron("30 2 * * *")
    # America/New_York は 2026-03-08 に 02:00→03:00 へ飛ぶ。02:30 はこの日だけ存在しない。
    occurrences = upcoming_occurrences(
        expression, after=_utc("2026-03-07 00:00"), timezone="America/New_York", count=2
    )

    # 03-07 の 02:30 EST = 07:30 UTC、次は 03-08 を飛ばして 03-09 の 02:30 EDT = 06:30 UTC。
    assert occurrences == [_utc("2026-03-07 07:30"), _utc("2026-03-09 06:30")]


def test_fall_back_repeated_local_time_fires_only_once() -> None:
    """秋の重複する時刻は前半の一回だけ発火する (§22 D3)。

    両方採ると毎年秋に一回余分に走る。
    """

    expression = parse_cron("30 1 * * *")
    # America/New_York は 2026-11-01 に 02:00→01:00 へ戻る。現地 01:30 は二度現れる。
    occurrences = upcoming_occurrences(
        expression, after=_utc("2026-10-31 00:00"), timezone="America/New_York", count=3
    )

    # 10-31 は EDT (05:30 UTC)、11-01 は fold=0 の EDT 側 (05:30 UTC) だけ、
    # 11-02 は EST (06:30 UTC)。
    assert occurrences == [
        _utc("2026-10-31 05:30"),
        _utc("2026-11-01 05:30"),
        _utc("2026-11-02 06:30"),
    ]
    # 重複側 (06:30 UTC の 11-01) は候補に入らない。
    assert _utc("2026-11-01 06:30") not in occurrences


def test_unsatisfiable_expression_returns_none_instead_of_looping() -> None:
    """一致し得ない式は無限探索せず None を返すことを確認する。"""

    expression = parse_cron("0 0 30 2 *")

    assert next_occurrence(expression, after=_utc("2026-01-01 00:00"), timezone="UTC") is None


def test_upcoming_occurrences_are_strictly_increasing() -> None:
    """連続する発火時刻が厳密に増加することを確認する。"""

    expression = parse_cron("*/15 * * * *")
    occurrences = upcoming_occurrences(
        expression, after=_utc("2026-07-26 09:07"), timezone="UTC", count=4
    )

    assert occurrences == [
        _utc("2026-07-26 09:15"),
        _utc("2026-07-26 09:30"),
        _utc("2026-07-26 09:45"),
        _utc("2026-07-26 10:00"),
    ]


def test_unknown_timezone_is_rejected() -> None:
    """未知の timezone 名を保存させないことを確認する。"""

    with pytest.raises(CronExpressionError):
        normalize_timezone("Mars/Olympus")


def test_known_timezone_round_trips() -> None:
    """既知の IANA 名はそのまま返ることを確認する。"""

    assert normalize_timezone("Asia/Tokyo") == "Asia/Tokyo"
