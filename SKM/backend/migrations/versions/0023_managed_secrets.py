"""MANAGED SecretReference の envelope 暗号化された密文 table を追加する。

Revision ID: 0023_managed_secrets
Revises: 0022_user_ui_language
Create Date: 2026-07-20

docs/01 §18 / docs/09 §7.2。明文は保存せず、KEK で封入した密文・nonce・KEK version だけを
公開 read model と物理分離した table に保持する。secret_references の resolver CHECK を
MANAGED まで緩める。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_managed_secrets"
down_revision: str | None = "0022_user_ui_language"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """resolver CHECK を MANAGED まで緩め、密文専用 table を追加する。"""

    # Postgres は CHECK を直接変更できないため drop→再作成で MANAGED を許可する。
    op.drop_constraint("secret_references_resolver", "secret_references", type_="check")
    op.create_check_constraint(
        "secret_references_resolver",
        "secret_references",
        "resolver IN ('ENVIRONMENT', 'FILE', 'MANAGED')",
    )

    op.create_table(
        "managed_secret_material",
        sa.Column("secret_reference_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("kek_version", sa.String(length=32), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["secret_reference_id"],
            ["secret_references.id"],
            # 規約名は 63 byte を超えるため、model と同じ短い FK 名を明示する。
            name="fk_managed_secret_material_reference",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_managed_secret_material_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_managed_secret_material")),
        sa.UniqueConstraint(
            "secret_reference_id",
            name="uq_managed_secret_material_secret_reference_id",
        ),
    )
    op.create_index(
        op.f("ix_managed_secret_material_project_id"),
        "managed_secret_material",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    """密文 table を落とし、resolver CHECK を MANAGED 除外へ戻す。

    残存する MANAGED 行があると CHECK 復元に失敗する。運用は事前に MANAGED SecretReference を
    退避してから downgrade する。
    """

    op.drop_index(
        op.f("ix_managed_secret_material_project_id"),
        table_name="managed_secret_material",
    )
    op.drop_table("managed_secret_material")
    op.drop_constraint("secret_references_resolver", "secret_references", type_="check")
    op.create_check_constraint(
        "secret_references_resolver",
        "secret_references",
        "resolver IN ('ENVIRONMENT', 'FILE')",
    )
