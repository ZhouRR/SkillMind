"""認証 v2 の DDL/失効処理が model と一致し、rollback で旧会話を復活させない。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from sqlalchemy import CheckConstraint

from projectmind.db.models import AuthSession


def migration_module() -> ModuleType:
    """実 DB を変更せず、この revision だけを import する。"""

    path = (
        Path(__file__).resolve().parents[2] / "migrations/versions/0031_auth_session_credentials.py"
    )
    spec = importlib.util.spec_from_file_location("auth_session_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_auth_session_upgrade_matches_columns_constraints_and_revokes_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """版/role の列と制約を同期し、旧活動会話を削除せず失効させる。"""

    migration, operations = migration_module(), Mock()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    model = AuthSession.__table__
    for call in operations.add_column.call_args_list:
        table, column = call.args
        assert table == "auth_sessions"
        expected = model.c[column.name]
        assert str(column.type) == str(expected.type)
        assert column.nullable == expected.nullable
        if column.server_default is not None:
            assert expected.server_default is not None
            assert str(column.server_default.arg) == str(expected.server_default.arg) == "1"
    assert len(operations.add_column.call_args_list) == 2
    checks = {
        item.name: str(item.sqltext)
        for item in model.constraints
        if isinstance(item, CheckConstraint)
    }
    for call in operations.create_check_constraint.call_args_list:
        name, table, condition = call.args
        assert table == "auth_sessions"
        assert checks[f"ck_auth_sessions_{name}"] == condition
    assert len(operations.create_check_constraint.call_args_list) == 3
    assert operations.execute.call_args.args[0] == (
        "UPDATE auth_sessions SET revoked_at = CURRENT_TIMESTAMP "
        "WHERE revoked_at IS NULL AND credential_version = 1"
    )
    operations.drop_table.assert_not_called()


@pytest.mark.parametrize("blocked", [False, True])
def test_auth_session_downgrade_preserves_v2_audit(
    monkeypatch: pytest.MonkeyPatch,
    blocked: bool,
) -> None:
    """失効済みを含む v2 行がある場合、列を落とす前に停止する。"""

    migration, operations = migration_module(), Mock()
    operations.f.side_effect = str
    monkeypatch.setattr(migration, "op", operations)
    if blocked:
        operations.execute.side_effect = RuntimeError("preserve v2 audit")
        with pytest.raises(RuntimeError, match="preserve"):
            migration.downgrade()
        operations.drop_column.assert_not_called()
        operations.drop_constraint.assert_not_called()
    else:
        migration.downgrade()
    assert operations.method_calls[0][0] == "execute"
    sql = operations.execute.call_args.args[0]
    assert "WHERE credential_version = 2" in sql
    assert "must be preserved before downgrade" in sql
    assert "revoked_at IS NULL" not in sql
    if blocked:
        return
    expected = {
        item.name for item in AuthSession.__table__.constraints if isinstance(item, CheckConstraint)
    }
    assert {call.args[0] for call in operations.drop_constraint.call_args_list} == expected
    assert [call.args for call in operations.drop_column.call_args_list] == [
        ("auth_sessions", "system_role_at_login"),
        ("auth_sessions", "credential_version"),
    ]
    operations.drop_table.assert_not_called()
