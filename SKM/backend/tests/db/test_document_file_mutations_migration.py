"""新規目录回执と更新前条件が migration/model 間で一致することを確認する。"""

from __future__ import annotations

from unittest.mock import Mock

import sqlalchemy as sa

from skillmind.db.base import Base
from skillmind.db.models import ChangeProposal, DocumentMutationReceipt, ProjectDocumentEffectUpload
from tests.db.test_document_effect_migration import migration
from tests.db.test_input_snapshot_migration import _contract


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
