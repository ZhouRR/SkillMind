"""文書庫提案の nullable 制約と降級拒否を migration/model の両方で確認する。"""

from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from skillmind.db.base import Base
from skillmind.db.models import ChangeProposal


def migration_sql(action):
    """本番と同じ命名規約で実 revision を PostgreSQL SQL に展開する。接続は開かない。"""

    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/0046_document_library_proposals.py"
    )
    spec = importlib.util.spec_from_file_location("document_proposals_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": output,
            "target_metadata": Base.metadata,
        },
    )
    module.op = Operations(context)
    getattr(module, action)()
    return output.getvalue()


def test_migration_uses_exact_model_constraint_and_refuses_nonempty_downgrade():
    """二重命名や偽の backfill を防ぎ、nullable 戻しより先に元提案の存在を検査する。"""

    constraint = next(
        item
        for item in ChangeProposal.__table__.constraints
        if item.name == "ck_change_proposals_document_library_integration"
    )
    assert ChangeProposal.__table__.c.integration_id.nullable
    upgrade = migration_sql("upgrade")
    assert "DROP NOT NULL" in upgrade
    assert str(constraint.sqltext) in upgrade
    assert "ck_change_proposals_ck_" not in upgrade
    assert "ADD CONSTRAINT ck_change_proposals_document_library_integration" in upgrade
    downgrade = migration_sql("downgrade")
    assert (
        downgrade.index("ACCESS EXCLUSIVE")
        < downgrade.index("RAISE EXCEPTION")
        < downgrade.index("SET NOT NULL")
    )


@pytest.mark.parametrize(
    "capability,operation,has_integration,valid",
    [
        ("document.write/v1", "CREATE", False, True),
        ("document.write/v1", "CREATE", True, False),
        ("document.write/v1", "UPDATE", False, False),
        ("database.write/v1", "INSERT", False, False),
        ("database.write/v1", "INSERT", True, True),
        ("repository.write/v1", "commit", False, False),
    ],
)
def test_model_constraint_restricts_null_to_document_create(
    capability, operation, has_integration, valid
):
    """SQLite で CHECK の NULL 真理値を確認する。PG lock/FK の検証は別途行う。"""

    original = next(
        item
        for item in ChangeProposal.__table__.constraints
        if item.name == "ck_change_proposals_document_library_integration"
    )
    metadata = sa.MetaData()
    table = sa.Table(
        "proposals",
        metadata,
        sa.Column("capability_version", sa.Text, nullable=False),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("integration_id", sa.Uuid),
        sa.CheckConstraint(str(original.sqltext)),
    )
    engine = sa.create_engine("sqlite://")
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            command = table.insert().values(
                capability_version=capability,
                operation=operation,
                integration_id=uuid4() if has_integration else None,
            )
            if valid:
                connection.execute(command)
            else:
                with pytest.raises(sa.exc.IntegrityError):
                    connection.execute(command)
    finally:
        engine.dispose()
