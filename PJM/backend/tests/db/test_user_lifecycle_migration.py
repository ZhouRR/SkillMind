"""User 監査を残したまま downgrade が進まない lock/DDL 順序を検証する。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("has_audit", [True, False])
def test_user_downgrade_locks_user_before_audit_and_checks_before_destructive_ddl(
    monkeypatch: pytest.MonkeyPatch, has_audit: bool,
) -> None:
    """User→event writer と順序を揃え、存在確認後の INSERT を同じ DDL transaction で止める。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions/0032_user_lifecycle.py"
    spec = importlib.util.spec_from_file_location("user_lifecycle_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = Mock()
    monkeypatch.setattr(migration, "op", operations)
    failure = RuntimeError("test audit exists")
    operations.execute.side_effect = [None, None, failure if has_audit else None]

    if has_audit:
        with pytest.raises(RuntimeError, match="test audit exists"):
            migration.downgrade()
        operations.drop_table.assert_not_called()
        operations.drop_column.assert_not_called()
        operations.drop_constraint.assert_not_called()
        operations.drop_index.assert_not_called()
    else:
        migration.downgrade()
        operations.drop_table.assert_called_once_with("user_security_events")
        operations.drop_column.assert_called_once_with("users", "row_version")
    statements = [call.args[0] for call in operations.execute.call_args_list]
    assert statements[:2] == [
        "LOCK TABLE users IN ACCESS EXCLUSIVE MODE",
        "LOCK TABLE user_security_events IN ACCESS EXCLUSIVE MODE",
    ]
    assert "IF EXISTS (SELECT 1 FROM user_security_events)" in statements[2]
    assert "RAISE EXCEPTION" in statements[2]
