"""Module 束縛の拒否理由が対処別に区別されることを検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from projectmind.compositions.repository import _binding_rejection, _is_bindable


def test_published_and_enabled_version_is_bindable() -> None:
    """PUBLISHED かつ Project 有効化済み(未無効化)だけが束縛可能。"""

    assert _is_bindable(("PUBLISHED", uuid4(), None)) is True


def test_unknown_version_reports_missing_asset() -> None:
    """Organization に存在しない version は「見つからない」と伝える。"""

    assert _is_bindable(None) is False
    assert "not found" in _binding_rejection(None)


def test_unpublished_version_reports_its_actual_status() -> None:
    """未発行は現在 status を添えて返し、利用者が発行操作へ向かえるようにする。

    廃止済み版本を束縛したままの module 更新がここへ来る。旧文言は "not published" 一択で、
    実際には「廃止済み」でも「未有効化」でも同じ表示になり原因が判らなかった。
    """

    rejection = _binding_rejection(("DEPRECATED", uuid4(), None))

    assert "not PUBLISHED" in rejection
    assert "DEPRECATED" in rejection


def test_version_without_enablement_row_reports_project_enablement() -> None:
    """PUBLISHED でも Project へ未有効化なら、Skill library での有効化を促す。"""

    rejection = _binding_rejection(("PUBLISHED", None, None))

    assert rejection == "not enabled for this project"


def test_disabled_enablement_is_distinguished_from_never_enabled() -> None:
    """一度有効化して無効化した版は「未有効化」と区別する。"""

    rejection = _binding_rejection(
        ("PUBLISHED", uuid4(), datetime(2026, 7, 21, tzinfo=UTC))
    )

    assert rejection == "disabled for this project"
