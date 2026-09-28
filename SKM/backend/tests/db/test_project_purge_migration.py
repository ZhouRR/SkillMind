"""Project 完全削除の FK 分離と監査を失わない回退境界を検証する。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

from skillmind.db.models import Base


@pytest.mark.parametrize("used", [False, True])
def test_project_purge_migration_detaches_only_original_identity_links(monkeypatch, used):
    """upload の複合 FK は維持し、実削除の監査があれば downgrade を拒否する。"""
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0057_project_purge.py"
    spec = importlib.util.spec_from_file_location("project_purge_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = Mock()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert migration.down_revision == "0056_inline_effect_owner"
    for table, column, parent in migration.DETACHED_REFERENCES:
        assert table in Base.metadata.tables
        assert f"{parent}.id" not in {
            fk.target_fullname for fk in Base.metadata.tables[table].c[column].foreign_keys
        }
    assert operations.drop_constraint.call_count == 6
    operations.execute.assert_not_called()
    operations.reset_mock()
    operations.execute.side_effect = [None, RuntimeError("audit retained") if used else None]
    if used:
        with pytest.raises(RuntimeError, match="audit retained"):
            migration.downgrade()
        operations.drop_table.assert_not_called()
        operations.create_foreign_key.assert_not_called()
    else:
        migration.downgrade()
        operations.drop_table.assert_called_once_with("project_deletion_audits")
        assert operations.create_foreign_key.call_count == 6
    assert "LOCK TABLE" in operations.execute.call_args_list[0].args[0]
    assert "RAISE EXCEPTION" in operations.execute.call_args_list[1].args[0]
