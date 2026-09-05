"""Infrastructure preflight の automation contract を検証する。"""

from __future__ import annotations

from projectmind.ops.preflight import exit_code


def test_preflight_exit_code_follows_readiness() -> None:
    """PostgreSQL/Redis のいずれかが未準備な report を成功扱いしない。"""

    assert exit_code({"status": "ready"}) == 0
    assert exit_code({"status": "not_ready"}) == 1
