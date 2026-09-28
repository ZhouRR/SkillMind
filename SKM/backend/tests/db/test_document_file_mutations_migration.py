"""新規目录回执と更新前条件が migration/model 間で一致することを確認する。"""

from __future__ import annotations

from io import StringIO
from unittest.mock import Mock

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql

from skillmind.db.base import Base
from skillmind.db.models import ChangeProposal, DocumentMutationReceipt, ProjectDocumentEffectUpload
from tests.db.test_document_effect_migration import migration
from tests.db.test_input_snapshot_migration import _contract


def test_upgrade_replaces_legacy_named_check_and_validates_existing_rows(monkeypatch):
    """旧 0046 の二重命名を実 dialect で再現し、新 CHECK への置換を検証する。"""
    original_name = "ck_change_proposals_document_library_integration"
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    old_table = sa.Table(
        "change_proposals",
        metadata,
        sa.Column("integration_id", sa.Uuid()),
        sa.CheckConstraint("integration_id IS NULL", name=original_name),
    )
    old_check = next(item for item in old_table.constraints if isinstance(item, sa.CheckConstraint))
    dialect = postgresql.dialect()
    legacy_name = dialect.identifier_preparer.format_constraint(old_check)
    assert legacy_name != original_name

    output = StringIO()
    context = MigrationContext.configure(
        dialect=dialect,
        opts={"as_sql": True, "output_buffer": output, "target_metadata": Base.metadata},
    )
    module = migration("0058_document_file_mutations.py")
    monkeypatch.setattr(module, "op", Operations(context))
    module.upgrade()
    sql = output.getvalue()
    canonical_drop = f"ALTER TABLE change_proposals DROP CONSTRAINT IF EXISTS {original_name};"
    legacy_drop = f"ALTER TABLE change_proposals DROP CONSTRAINT IF EXISTS {legacy_name};"
    create_check = f"ALTER TABLE change_proposals ADD CONSTRAINT {original_name} CHECK"
    assert sql.index(canonical_drop) < sql.index(create_check)
    assert sql.index(legacy_drop) < sql.index(create_check)
    assert sql.count("DROP CONSTRAINT") == 2
    # 既存の不正行を黙認せず、データや他の CHECK は変更しない。
    assert "NOT VALID" not in sql
    assert "DELETE FROM" not in sql and "UPDATE change_proposals" not in sql


def test_receipt_schema_and_nullable_replacement_columns_match_model(monkeypatch):
    """旧文書/Effect を変更せず追加する型・FK・一意性を比較する。"""
    module = migration("0058_document_file_mutations.py")
    monkeypatch.setattr(module, "op", Mock())
    module.op.f.side_effect = sa.schema.conv
    module.upgrade()
    constraint = next(
        item
        for item in ChangeProposal.__table__.constraints
        if item.name == "ck_change_proposals_document_library_integration"
    )
    assert module.op.create_check_constraint.call_args.args[2] == str(constraint.sqltext)
    assert module.down_revision == "0057_project_purge"
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for parent in ("effect_executions", "runs", "projects"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    name, *items = module.op.create_table.call_args.args
    table = sa.Table(name, metadata, *items)
    assert _contract(table) == _contract(DocumentMutationReceipt.__table__)
    for call in module.op.add_column.call_args_list:
        target, column = call.args
        assert target == "document_effect_uploads" and column.nullable
        expected = ProjectDocumentEffectUpload.__table__.c[column.name]
        assert str(column.type) == str(expected.type)
        assert not column.foreign_keys and column.server_default is None
    assert len(module.op.add_column.call_args_list) == 3
    module.op.execute.assert_not_called()


def test_downgrade_guards_committed_history_before_dropping_columns(monkeypatch):
    """原回执が存在する環境では、互換性のない巻戻しを静かに通さない。"""
    module = migration("0058_document_file_mutations.py")
    monkeypatch.setattr(module, "op", Mock())
    module.downgrade()
    operations = [call[0] for call in module.op.mock_calls]
    assert operations[:2] == ["execute", "execute"]
    assert "EXISTS" in module.op.execute.call_args_list[1].args[0]
    assert "replaces_document_id" in module.op.execute.call_args_list[1].args[0]
