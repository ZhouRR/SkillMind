"""所属監査の migration/metadata と SQL CHECK を外部 DB なしで照合する。"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import CheckConstraint

from skillmind.db.models import ProjectMemberEvent


def test_member_audit_foreign_keys_are_restrict_and_no_credential_columns_exist() -> None:
    """Project/Member/User の参照を残し、削除 CASCADE と秘密の自由列を許可しない。"""

    table = ProjectMemberEvent.__table__
    assert {key.target_fullname for key in table.foreign_keys} == {
        "organizations.id", "projects.id", "project_members.id", "users.id",
    }
    assert {key.ondelete for key in table.foreign_keys} == {"RESTRICT"}
    assert set(table.c.keys()) == {
        "id", "organization_id", "project_id", "member_id", "user_id", "actor_id",
        "action", "previous_status", "previous_joined_at", "status", "joined_at",
        "request_id", "created_at",
    }


@pytest.mark.parametrize(
    ("action", "previous_status", "previous_joined_at", "status", "valid"),
    [
        ("ADDED", None, None, "ACTIVE", True),
        ("ADDED", "REMOVED", "before", "ACTIVE", True),
        ("REMOVED", "ACTIVE", "before", "REMOVED", True),
        ("REMOVED", None, None, "REMOVED", False),
        ("ADDED", "ACTIVE", "before", "ACTIVE", False),
        ("ADDED", None, "before", "ACTIVE", False),
        ("ADDED", "REMOVED", None, "ACTIVE", False),
        ("UNKNOWN", None, None, "ACTIVE", False),
        ("REMOVED", "REMOVED", "before", "REMOVED", False),
    ],
)
def test_transition_checks_reject_null_removal_and_noop_history(
    action: str, previous_status: str | None, previous_joined_at: str | None,
    status: str, valid: bool,
) -> None:
    """CHECK は UNKNOWN も通すため明示 NULL 拒否を試す。SQLite は PG lock の証拠ではない。"""

    checks = [str(check.sqltext) for check in ProjectMemberEvent.__table__.constraints
              if isinstance(check, CheckConstraint)]
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE member_check (action TEXT NOT NULL, previous_status TEXT, "
            "previous_joined_at TEXT, status TEXT NOT NULL, "
            + ", ".join(f"CHECK ({check})" for check in checks) + ")"
        )
        values = (action, previous_status, previous_joined_at, status)
        if valid:
            connection.execute("INSERT INTO member_check VALUES (?, ?, ?, ?)", values)
        else:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO member_check VALUES (?, ?, ?, ?)", values)


def test_migration_matches_checks_does_not_backfill_and_guards_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DDL 呼出を観測し、架空履歴の補造や監査存在下の table drop を防ぐ順序を確認する。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions/0033_project_member_audit.py"
    spec = importlib.util.spec_from_file_location("member_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = Mock()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert migration.down_revision == "0032_user_lifecycle"
    operations.execute.assert_not_called()
    constraints = operations.create_table.call_args.args[1:]
    assert {str(item.sqltext) for item in constraints if isinstance(item, CheckConstraint)} == {
        str(item.sqltext) for item in ProjectMemberEvent.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    operations.reset_mock()
    failure = RuntimeError("test existing audit guard")
    operations.execute.side_effect = [None, failure]
    with pytest.raises(RuntimeError, match="existing audit guard"):
        migration.downgrade()
    guard = operations.execute.call_args.args[0]
    assert operations.execute.call_args_list[0].args[0] == (
        "LOCK TABLE project_member_events IN ACCESS EXCLUSIVE MODE"
    )
    assert "IF EXISTS (SELECT 1 FROM project_member_events)" in guard
    assert "RAISE EXCEPTION" in guard
    operations.drop_table.assert_not_called()
    operations.drop_index.assert_not_called()
