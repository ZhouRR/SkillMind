"""JSON の正常な空白・escape と、無限空白の境界を区別する。"""

from __future__ import annotations

import json

import pytest
from skillmind.agent.json_output_guard import JSON_WHITESPACE_LIMIT, JsonWhitespaceGuard


@pytest.mark.parametrize("prefix", ["", '{"ok":', '{"ok":true}'])
def test_consecutive_whitespace_is_bounded_across_delta_chunks(prefix: str) -> None:
    """先頭・値待ち・末尾の空白に同じ上限を適用する。"""

    guard = JsonWhitespaceGuard()
    assert not guard.exceeded(prefix)
    for _ in range(JSON_WHITESPACE_LIMIT // 4 - 1):
        assert not guard.exceeded(" \t\r\n")
    assert guard.exceeded(" \t\r\n")


def test_formatted_json_resets_the_run_at_meaningful_characters() -> None:
    """総空白量が大きくても、要素を生成し続ける JSON は止めない。"""

    guard = JsonWhitespaceGuard()
    payload = json.dumps({"values": list(range(3000))}, indent=2)
    assert sum(c.isspace() for c in payload) > JSON_WHITESPACE_LIMIT
    assert not any(guard.exceeded(c) for c in payload)


def test_string_whitespace_and_split_escapes_remain_original_business_values() -> None:
    """JSON encoded 値の escaped quote と長い文字列空白を破損・停止させない。"""

    guard = JsonWhitespaceGuard()
    payload = json.dumps({'value': '\\"' + ' ' * (JSON_WHITESPACE_LIMIT * 2) + '\nend'})
    assert not any(guard.exceeded(c) for c in payload)
    assert guard.exceeded(' ' * JSON_WHITESPACE_LIMIT)
