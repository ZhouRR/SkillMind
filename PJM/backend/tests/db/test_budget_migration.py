"""予算の model/DDL 同期と、既存勘定を消さない回退門禁を検証する。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from projectmind.db.models import RunBudgetAccount, RunBudgetReceipt, RunBudgetReservation
from tests.db.test_input_snapshot_migration import _contract


def migration_module() -> ModuleType:
    """実 DB へ接続せず、凍結した migration 定義を読み取る。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions/0030_run_budget_ledger.py"
    spec = importlib.util.spec_from_file_location("budget_migration_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_budget_migration_matches_the_three_persistent_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """金額精度、欠測の NULL、元鍵一意、RESTRICT と停止/結算の制約を一致させる。"""

    migration = migration_module()
    operations = Mock()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert operations.create_table.call_count == 3
    models = {
        model.__table__.name: model.__table__
        for model in (RunBudgetAccount, RunBudgetReservation, RunBudgetReceipt)
    }
    metadata = sa.MetaData(naming_convention=RunBudgetAccount.metadata.naming_convention)
    for parent in ("runs", "run_segments", "run_attempts"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for call in operations.create_table.call_args_list:
        name, *elements = call.args
        migrated = sa.Table(name, metadata, *elements)
        assert _contract(migrated) == _contract(models[name])
    operations.create_index.assert_called_once_with(
        "ix_run_budget_group", "run_budget_reservations", ["run_id", "group_key"]
    )
    operations.execute.assert_not_called()


@pytest.mark.parametrize("blocked", [False, True])
def test_budget_downgrade_keeps_ledger_and_unknown_obligations(
    monkeypatch: pytest.MonkeyPatch, blocked: bool
) -> None:
    """空表だけが削除でき、報告一件でもある場合は drop を呼ぶ前に止まる。"""

    migration, operations = migration_module(), Mock()
    monkeypatch.setattr(migration, "op", operations)
    if blocked:
        operations.execute.side_effect = RuntimeError("preserve ledger")
        with pytest.raises(RuntimeError, match="preserve"):
            migration.downgrade()
        operations.drop_table.assert_not_called()
        operations.drop_index.assert_not_called()
    else:
        migration.downgrade()
        assert [call.args[0] for call in operations.drop_table.call_args_list] == [
            "run_budget_receipts",
            "run_budget_reservations",
            "run_budget_accounts",
        ]
    sql = str(operations.execute.call_args.args[0])
    assert "must be preserved before downgrade" in sql
    for name in ("run_budget_accounts", "run_budget_reservations", "run_budget_receipts"):
        assert f"SELECT 1 FROM {name}" in sql
