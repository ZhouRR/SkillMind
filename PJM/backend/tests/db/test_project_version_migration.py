"""Project 版の初期化、整数範囲と使用済み版を消さない downgrade DDL 順序を固定する。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import CheckConstraint

from projectmind.db.models import Project


@pytest.mark.parametrize("used", [True, False])
def test_project_version_migration_initializes_one_and_locks_before_downgrade_check(
    monkeypatch: pytest.MonkeyPatch, used: bool,
) -> None:
    """実 DB は使わず、過去の編集回数を補造しない DDL と回退拒否の呼出順を確認する。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions/0034_project_row_version.py"
    spec = importlib.util.spec_from_file_location("project_version_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = Mock()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert migration.down_revision == "0033_project_member_audit"
    column = operations.add_column.call_args.args[1]
    assert column.name == "row_version" and str(column.server_default.arg) == "1"
    assert column.nullable is False
    operations.execute.assert_not_called()
    expected_check = "row_version >= 1 AND row_version <= 2147483647"
    assert operations.create_check_constraint.call_args.args == (
        "projects_row_version_range", "projects", expected_check,
    )
    assert expected_check in {
        str(item.sqltext) for item in Project.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    operations.reset_mock()
    operations.execute.side_effect = [None, RuntimeError("test used version") if used else None]
    if used:
        with pytest.raises(RuntimeError, match="test used version"):
            migration.downgrade()
        operations.drop_column.assert_not_called()
        operations.drop_constraint.assert_not_called()
    else:
        migration.downgrade()
        operations.drop_column.assert_called_once_with("projects", "row_version")
    assert operations.execute.call_args_list[0].args[0] == (
        "LOCK TABLE projects IN ACCESS EXCLUSIVE MODE"
    )
    guard = operations.execute.call_args_list[1].args[0]
    assert "IF EXISTS (SELECT 1 FROM projects WHERE row_version > 1)" in guard
    assert "RAISE EXCEPTION" in guard
