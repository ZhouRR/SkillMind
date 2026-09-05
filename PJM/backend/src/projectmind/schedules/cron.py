"""5 段 cron 式の解析と IANA timezone 上での次回発火時刻の算出 (計画 §22 D2/D3)。

外部依存 (`croniter` 等) を入れずに標準库だけで実装する。ここが扱うのは「式を集合へ展開する」
「次の一致時刻を探す」という決定的な純関数であり、全分岐をテストで固定できる。調度の要は
時刻計算の正しさそのものなので、供給链を広げるより手元で検証可能にする方を採る
(`agent/binary_text.py` と同じ取捨)。

DST の二つの曖昧点は §22 D3 で明示的に決めてある。

- 春の**存在しない**現地時刻 (跳ばされた 1 時間) は、その回を**飛ばす**。近傍の時刻へ繰り上げると
  「毎日 02:30」がその日だけ不定の時刻になる。
- 秋の**重複する**現地時刻は `fold=0` の一回だけ発火させる。両方採ると毎年秋に一回余分に走る。

判定は「現地時刻 → UTC → 現地時刻」と往復させ、壁時計が戻ってくるかどうかで行う。PEP 495 では
存在しない時刻も例外にならず遷移前の offset で解釈されるため、往復しないと検知できない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "CronExpression",
    "CronExpressionError",
    "next_occurrence",
    "normalize_timezone",
    "parse_cron",
    "upcoming_occurrences",
]

# 「一致し得ない式」(例: 2 月 30 日) を無限探索させないための上限。閏年 4 回分を見て見つからない
# 式は保存段階で拒否する。
_MAX_SEARCH_DAYS = 366 * 4

_FIELD_BOUNDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 7),
)

_MAX_EXPRESSION_LENGTH = 128


class CronExpressionError(ValueError):
    """cron 式が本平台の受け付ける文法に収まらないことを表す。"""


@dataclass(frozen=True, slots=True)
class CronExpression:
    """展開済みの 5 段 cron 式。`expression` は正規化した元文字列。"""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    # POSIX の day-of-month / day-of-week は「両方とも `*` 以外なら OR」という特例を持つ。
    # 集合が全域かどうかでは代用できない (`1-31` と `*` は同じ集合だが特例の扱いが違う) ため、
    # 元の field が literal `*` だったかを保持する。
    day_of_month_restricted: bool
    day_of_week_restricted: bool


def parse_cron(expression: str) -> CronExpression:
    """`分 時 日 月 曜` の 5 段式を解析する。未対応の拡張文法は保存段階で拒否する。

    `L` / `W` / `#` / `@reboot` のような拡張は受け付けない。黙って別の意味に解釈すると、利用者が
    書いたつもりの時刻と実際の発火時刻が食い違ったまま運用され続ける。
    """

    if len(expression) > _MAX_EXPRESSION_LENGTH:
        raise CronExpressionError("Cron expression is too long")
    fields = expression.split()
    if len(fields) != 5:
        raise CronExpressionError("Cron expression must have exactly 5 fields")
    values: list[frozenset[int]] = []
    for field, (name, low, high) in zip(fields, _FIELD_BOUNDS, strict=True):
        values.append(_parse_field(field, name=name, low=low, high=high))
    # cron の曜日は 0 と 7 の双方が日曜。内部では 0 に寄せる。
    days_of_week = frozenset(0 if value == 7 else value for value in values[4])
    return CronExpression(
        expression=" ".join(fields),
        minutes=values[0],
        hours=values[1],
        days_of_month=values[2],
        months=values[3],
        days_of_week=days_of_week,
        day_of_month_restricted=fields[2] != "*",
        day_of_week_restricted=fields[4] != "*",
    )


def normalize_timezone(name: str) -> str:
    """IANA timezone 名を検証して返す。未知の名前は保存させない。"""

    if not name or len(name) > 64:
        raise CronExpressionError("Timezone name is invalid")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise CronExpressionError("Timezone name is not a known IANA zone") from error
    return name


def next_occurrence(
    expression: CronExpression,
    *,
    after: datetime,
    timezone: str,
) -> datetime | None:
    """`after` より厳密に後の最初の発火時刻を UTC で返す。一致し得ない式では `None`。

    候補は現地時刻の昇順で走査する。存在しない現地時刻は D3 に従って飛ばすため、採用される候補の
    UTC 時刻は単調に増加する。
    """

    zone = ZoneInfo(timezone)
    boundary = after.astimezone(UTC).replace(second=0, microsecond=0)
    local_after = after.astimezone(zone)
    cursor = local_after.date()
    hours = sorted(expression.hours)
    minutes = sorted(expression.minutes)
    for _ in range(_MAX_SEARCH_DAYS):
        if _matches_date(expression, cursor):
            for hour in hours:
                for minute in minutes:
                    resolved = _resolve_local(
                        datetime.combine(cursor, time(hour, minute)), zone
                    )
                    if resolved is not None and resolved > boundary:
                        return resolved
        cursor += timedelta(days=1)
    return None


def upcoming_occurrences(
    expression: CronExpression,
    *,
    after: datetime,
    timezone: str,
    count: int,
) -> list[datetime]:
    """次の `count` 回分の発火時刻を返す。途中で尽きた場合は短い列を返す。

    保存前に少なくとも 3 回分を提示する要件 (`docs/07` §8.3) の計算元。
    """

    occurrences: list[datetime] = []
    cursor = after
    for _ in range(max(count, 0)):
        occurrence = next_occurrence(expression, after=cursor, timezone=timezone)
        if occurrence is None:
            break
        occurrences.append(occurrence)
        cursor = occurrence
    return occurrences


def _resolve_local(local_naive: datetime, zone: ZoneInfo) -> datetime | None:
    """現地の壁時計時刻を UTC へ解決する。存在しない時刻は `None`。

    PEP 495 では存在しない時刻も例外にならず遷移前 offset で解釈されるので、UTC へ変換した後に
    現地へ戻して壁時計が一致するかで検知する。`fold=0` 固定により、重複する時刻は前半の一回だけが
    候補になる。
    """

    aware = local_naive.replace(tzinfo=zone, fold=0)
    utc_value = aware.astimezone(UTC)
    round_trip = utc_value.astimezone(zone)
    if (
        round_trip.year,
        round_trip.month,
        round_trip.day,
        round_trip.hour,
        round_trip.minute,
    ) != (
        local_naive.year,
        local_naive.month,
        local_naive.day,
        local_naive.hour,
        local_naive.minute,
    ):
        return None
    return utc_value.replace(second=0, microsecond=0)


def _matches_date(expression: CronExpression, value: date) -> bool:
    """日付段 (月・日・曜) の一致を POSIX の OR 特例込みで判定する。"""

    if value.month not in expression.months:
        return False
    day_matches = value.day in expression.days_of_month
    # Python の weekday は月曜 0 始まり。cron は日曜 0 始まりなので ISO 曜日を 7 で剰余する。
    weekday_matches = (value.isoweekday() % 7) in expression.days_of_week
    if expression.day_of_month_restricted and expression.day_of_week_restricted:
        return day_matches or weekday_matches
    return day_matches and weekday_matches


def _parse_field(field: str, *, name: str, low: int, high: int) -> frozenset[int]:
    """一つの段を整数集合へ展開する。"""

    if not field:
        raise CronExpressionError(f"Cron field {name} is empty")
    values: set[int] = set()
    for part in field.split(","):
        values.update(_parse_part(part, name=name, low=low, high=high))
    if not values:
        raise CronExpressionError(f"Cron field {name} matches nothing")
    return frozenset(values)


def _parse_part(part: str, *, name: str, low: int, high: int) -> set[int]:
    """`*`、`n`、`a-b`、`*/n`、`a-b/n` のいずれかを展開する。"""

    body, _, step_text = part.partition("/")
    step = 1
    if step_text:
        if not step_text.isdigit():
            raise CronExpressionError(f"Cron field {name} has an invalid step")
        step = int(step_text)
        if step < 1 or step > high - low + 1:
            raise CronExpressionError(f"Cron field {name} step is out of range")
    if body == "*":
        start, end = low, high
    elif "-" in body:
        start_text, _, end_text = body.partition("-")
        start = _parse_number(start_text, name=name, low=low, high=high)
        end = _parse_number(end_text, name=name, low=low, high=high)
        if start > end:
            raise CronExpressionError(f"Cron field {name} range is inverted")
    else:
        start = _parse_number(body, name=name, low=low, high=high)
        # `5/2` は「5 から段の上限まで 2 刻み」と解する (広く使われる解釈)。step の無い単一値は
        # その値だけ。
        end = high if step_text else start
    return set(range(start, end + 1, step))


def _parse_number(text: str, *, name: str, low: int, high: int) -> int:
    """段の範囲に収まる十進数だけを受け付ける。"""

    if not text.isdigit():
        raise CronExpressionError(f"Cron field {name} is not a plain number")
    value = int(text)
    if value < low or value > high:
        raise CronExpressionError(f"Cron field {name} is out of range")
    return value
