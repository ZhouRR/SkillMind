"""続行 checkpoint の配列順・重複・置換を、追加と取り違えないことを検証する。"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from skillmind.agent.continuation_prompt import continuation_prompt
from tests.agent.test_continuation_prompt import compiled, context_with_brief


@pytest.mark.parametrize(
    "before,after,expected,replaced",
    [
        (["a"], ["a", "b"], ["b"], False),
        (["a"], ["a", "a"], ["a"], False),
        (["a", "b"], ["b", "a"], ["b", "a"], True),
        (["a", "b"], ["a", "x", "b"], ["a", "x", "b"], True),
        (["a", "b"], ["a"], ["a"], True),
        (["a"], [], [], True),
    ],
)
def test_continuation_requires_an_exact_prefix_for_list_additions(
    tmp_path,
    before,
    after,
    expected,
    replaced,
) -> None:
    """集合差では消える重複値や並べ替えも、元配列の意味を保って送信する。"""
    original = context_with_brief(tmp_path, checkpoint={"confirmed_facts": before})
    _, metadata = continuation_prompt(original, original.prompt, None)
    brief = deepcopy(original.task_brief)
    brief["identity"]["segment_no"] = 2
    brief["checkpoint"]["confirmed_facts"] = after
    current = compiled(original, brief)
    prompt, _ = continuation_prompt(current, current.prompt, metadata)
    raw = prompt.split("Audited continuation changes (JSON): ", 1)[1].split("\n", 1)[0]
    replacement = prompt.split("Replacement checkpoint lists (JSON): ", 1)[1].split("\n", 1)[0]
    assert json.loads(raw)["confirmed_facts"] == expected
    assert ("confirmed_facts" in json.loads(replacement)) is replaced
    assert "exact_table.exact_column" not in prompt
    assert original.task_brief["checkpoint"]["confirmed_facts"] == before


def test_malformed_list_hashes_fall_back_to_explicit_replacement(tmp_path) -> None:
    """比較用 metadata の不正要素を set に入れて実行を壊さない。"""
    original = context_with_brief(tmp_path, checkpoint={"confirmed_facts": ["a"]})
    _, metadata = continuation_prompt(original, original.prompt, None)
    metadata["checkpoint_hashes"]["confirmed_facts"] = [{"invalid": "hash"}]
    prompt, _ = continuation_prompt(original, original.prompt, metadata)
    assert 'Replacement checkpoint lists (JSON): ["confirmed_facts"]' in prompt
    assert '"confirmed_facts":["a"]' in prompt
