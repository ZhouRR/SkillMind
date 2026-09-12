"""directory 境界と実暦日/DST/cutoff を、storage や時計の mock なしで検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from skillmind.documents.directory_query import parse_directory_query
from tests.documents.fakes import document_content, document_snapshot


def test_directory_exclusions_and_recursion_do_not_match_neighbor_prefixes():
    """再帰選択は directory の区切りを守り、出力と一時ファイルだけを除外する。"""
    paths = [
        ("specs", "a.XLSX"),
        ("specs/sub", "b.xls"),
        ("specs", "notes.md"),
        ("specs2", "neighbor.xlsx"),
        ("specs/rv", "result.xlsx"),
        ("specs/rv-old", "keep.xlsx"),
        ("specs/sub", "~$temp.xlsx"),
    ]
    docs = document_snapshot(
        uuid4(), [document_content(b"x", folder=folder, name=name) for folder, name in paths]
    ).documents
    params = {
        "directory": "specs/",
        "extensions": [".xlsx", ".xls"],
        "exclude_directories": ["specs/rv/"],
        "exclude_name_prefixes": ["~$"],
    }
    assert [d.path for d in parse_directory_query(params).candidates(docs)] == [
        "specs/a.XLSX",
        "specs/rv-old/keep.xlsx",
        "specs/sub/b.xls",
    ]
    assert [
        d.path for d in parse_directory_query({**params, "recursive": False}).candidates(docs)
    ] == [
        "specs/a.XLSX",
    ]
    assert parse_directory_query({"directory": "", "recursive": False}).candidates(docs) == ()
    assert len(parse_directory_query({"directory": ""}).candidates(docs)) == len(paths)


@pytest.mark.parametrize(
    "day,zone,hours",
    [
        ("2026-09-11", "Asia/Tokyo", 24),
        ("2026-03-08", "America/New_York", 23),
        ("2026-11-01", "America/New_York", 25),
        ("2011-12-30", "Pacific/Apia", 0),
    ],
)
def test_calendar_window_handles_dst_and_skipped_day(day, zone, hours):
    """UTC の 24 時間を足さず、指定 timezone の隣り合う暦日境界を使う。"""
    query = parse_directory_query(
        {
            "directory": "",
            "modified_on": {
                "date": day,
                "timezone": zone,
                "not_after": "2026-12-01T00:00:00Z",
            },
        }
    )
    assert query.ends_at - query.starts_at == timedelta(hours=hours)
    assert not query.matches_time(query.starts_at - timedelta(microseconds=1))
    assert query.matches_time(query.starts_at) is bool(hours)
    assert not query.matches_time(query.ends_at)


def test_japan_midnight_and_inclusive_cutoff_are_distinct_boundaries():
    """当日開始は UTC 前日 15 時で、抽出基準時刻は等値を含む。"""
    query = parse_directory_query(
        {
            "directory": "",
            "modified_on": {
                "date": "2026-09-11",
                "timezone": "Asia/Tokyo",
                "not_after": "2026-09-11T10:00:00+09:00",
            },
        }
    )
    assert query.starts_at == datetime(2026, 9, 10, 15, tzinfo=UTC)
    assert query.matches_time(datetime(2026, 9, 11, 1, tzinfo=UTC))
    assert not query.matches_time(datetime(2026, 9, 11, 1, 0, 0, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    "change",
    [
        {"directory": None},
        {"directory": "/specs"},
        {"directory": "../specs"},
        {"directory": "specs//"},
        {"directory": "specs/./"},
        {"directory": "a\\b"},
        {"directory": ".skillmind"},
        {"directory": "specs\n"},
        {"recursive": 1},
        {"extensions": ["xlsx"]},
        {"extensions": [".xls", ".xls"]},
        {"exclude_directories": [""]},
        {"exclude_name_prefixes": ["a/b"]},
        {"modified_on": None},
        {"modified_on": {}},
    ],
)
def test_invalid_query_is_rejected_before_storage_access(change):
    """不明条件を既定値に置き換えず、path の正規化で越境を救済しない。"""
    with pytest.raises(ValueError):
        parse_directory_query({"directory": "", **change})


@pytest.mark.parametrize(
    "change",
    [
        {"date": "2026-02-30"},
        {"date": "9999-12-31"},
        {"date": "20260911"},
        {"timezone": "Unknown/Zone"},
        {"timezone": "../UTC"},
        {"not_after": "2026-09-11T12:00:00"},
        {"not_after": "2026-09-11"},
    ],
)
def test_invalid_calendar_filter_has_no_implicit_timezone(change):
    """timezone と aware cutoff は呼出側が指定し、業務既定値を共通 Tool に埋め込まない。"""
    with pytest.raises(ValueError):
        parse_directory_query(
            {
                "directory": "",
                "modified_on": {
                    "date": "2026-09-11",
                    "timezone": "Asia/Tokyo",
                    "not_after": "2026-09-11T12:00:00Z",
                    **change,
                },
            }
        )
