"""JSON 前処理が本文や候補の曖昧さを隠さないことを確認する。"""

from __future__ import annotations

import pytest

from skillmind.core.json_text import strip_json_object_preamble


@pytest.mark.parametrize("prefix", ["Done.\n", "完了。\r\n\r\n  ", "Progress `ref`.\n\n"])
def test_removes_only_plain_preamble(prefix: str) -> None:
    """独立行の object から後は一文字も変えない。"""

    body = '{"text":"{literal}","nested":{"list":[]}}\n'
    assert strip_json_object_preamble(prefix + body) == body


@pytest.mark.parametrize("raw", [
    '{"value":1}',
    'Inline {"value":1}',
    'First {}\n{"value":1}',
    'Array [\n{"value":1}\n]',
    'Report ]\n{"value":1}',
    'Report }\n{"value":1}',
    'Report\n```json\n{"value":1}\n```',
    'Report\nnot an object',
    'x' * 4097 + '\n{"value":1}',
])
def test_does_not_select_ambiguous_or_embedded_candidates(raw: str) -> None:
    """前言の条件が成立しなければ原文を保ち、後段の厳密 decode に委ねる。"""

    assert strip_json_object_preamble(raw) == raw
