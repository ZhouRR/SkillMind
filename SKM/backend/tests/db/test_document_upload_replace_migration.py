"""更新前条件を旧 upload と互換に追加し、使用済み監査の downgrade を防ぐ。"""

from __future__ import annotations

import sqlite3
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa

from skillmind.db.models import ProjectDocumentUpload
from tests.db.test_document_effect_migration import migration
from tests.db.test_document_upload_intent_migration import _insert, _row
from tests.db.test_document_upload_intent_migration import database as database


def test_upgrade_preserves_original_rows_and_matches_model(monkeypatch):
    """旧行は NULL のまま、現行 model と同じ条件・型を追加する。"""
    module = migration("0060_document_upload_replace.py")
    monkeypatch.setattr(module, "op", Mock())
    module.op.f.side_effect = sa.schema.conv
    module.upgrade()
    for call in module.op.add_column.call_args_list:
        table, column = call.args
        assert table == "document_upload_intents" and column.nullable
        assert str(column.type) == str(ProjectDocumentUpload.__table__.c[column.name].type)
    constraint = next(item for item in ProjectDocumentUpload.__table__.constraints
                      if item.name == "ck_document_upload_intents_replacement")
    assert module.op.create_check_constraint.call_args.args[2] == str(constraint.sqltext)
    module.op.execute.assert_not_called()


def test_downgrade_checks_history_before_dropping_columns(monkeypatch):
    """元対象の情報を捨てる前に、更新履歴がないことを lock 内で要求する。"""
    module = migration("0060_document_upload_replace.py")
    monkeypatch.setattr(module, "op", Mock())
    module.downgrade()
    assert [call[0] for call in module.op.mock_calls[:2]] == ["execute", "execute"]
    assert "WHERE replaces_document_id IS NOT NULL" in module.op.execute.call_args_list[1].args[0]


@pytest.mark.parametrize("identity,checksum", [
    (None, "sha256:" + "a" * 64), (str(UUID(int=99)), None),
    (str(UUID(int=0)), "sha256:" + "a" * 64),
    (str(UUID(int=4001)), "sha256:" + "a" * 64),
    (str(UUID(int=99)), "invalid"),
])
def test_database_rejects_incomplete_or_invalid_replacement(database, identity, checksum):
    """実 DDL の CHECK は片側 NULL、nil/自己参照、不正 hash を永続化させない。"""
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(database, _row(replaces_document_id=identity, expected_checksum=checksum))


def test_database_accepts_legacy_and_paired_replacement(database):
    """旧 NULL 行と正しい更新条件の双方を受け入れ、既存予約の互換を保つ。"""
    _insert(database, _row())
    _insert(database, _row(
        2, replaces_document_id=str(UUID(int=4001)), expected_checksum="sha256:" + "a" * 64,
    ))
