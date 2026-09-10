"""core/hashing と core/redaction の共通実装を検証する。"""

from __future__ import annotations

import pytest

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.json_text import strip_code_fence
from skillmind.core.redaction import find_sensitive_key
from skillmind.runs.domain import lease_token_hash


def test_strip_code_fence_unwraps_only_a_whole_wrapping_fence() -> None:
    """全体を包む単一 fence だけ剥がし、fence 無し・部分 fence は原文のまま返す。"""

    assert strip_code_fence('```json\n{"a":1}\n```') == '{"a":1}'
    assert strip_code_fence('```\n{"a":1}\n```') == '{"a":1}'
    # fence が無い普通の JSON はそのまま返す。
    assert strip_code_fence('{"a":1}') == '{"a":1}'
    # 本文中の ``` は「全体を包む」ではないので触らない (推測変換をしない不変条件)。
    assert strip_code_fence('prefix ```json\n{"a":1}\n```') == 'prefix ```json\n{"a":1}\n```'


def test_canonical_json_is_key_order_independent() -> None:
    """Key 順の異なる同一 object が同じ canonical 表現になることを確認する。"""

    assert canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == canonical_json(
        {"a": {"c": 3, "d": 2}, "b": 1}
    )
    assert canonical_json({"漢字": "值"}) == '{"漢字":"值"}'


def test_canonical_json_rejects_nan() -> None:
    """契約 hash の入力に NaN が混入した場合は保存前に失敗させる。"""

    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})


def test_sha256_hex_accepts_text_and_bytes() -> None:
    """文字列と bytes が同じ UTF-8 digest に解決されることを確認する。"""

    assert sha256_hex("abc") == sha256_hex(b"abc")
    assert len(sha256_hex("abc")) == 64


def test_lease_token_hash_matches_sha256_hex() -> None:
    """Lease token hash が共通 SHA-256 実装と一致することを確認する。"""

    assert lease_token_hash("lease-token") == sha256_hex("lease-token")


@pytest.mark.parametrize(
    "value",
    [
        {"api_key": "x"},
        {"outer": {"Authorization": "x"}},
        {"items": [{"nested": {"client_secret": "x"}}]},
        {"REFRESH_TOKEN": "x"},
        # MANAGED SecretReference の作成 request field。docs/09 §7.2 はこの遮断に依存する。
        {"secret_value": "x"},
    ],
)
def test_find_sensitive_key_detects_nested_credentials(value: dict[str, object]) -> None:
    """入れ子や大文字違いの credential field 名を検出することを確認する。"""

    assert find_sensitive_key(value) is not None


def test_find_sensitive_key_allows_plain_metadata() -> None:
    """通常の metadata が誤検出されないことを確認する。"""

    assert find_sensitive_key({"path": "src/a.py", "line": 1, "tags": ["a"]}) is None
