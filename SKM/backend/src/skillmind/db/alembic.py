"""Alembic 設定へ application の接続情報を渡す補助処理を定義する。"""

from __future__ import annotations


def escape_config_value(value: str) -> str:
    """ConfigParser の interpolation を避けるため percent 記号を escape する。"""

    return value.replace("%", "%%")
