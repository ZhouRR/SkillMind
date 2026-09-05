"""Bootstrap 時に播かれる JAF seed Project と Skill 版本を削除する。

Revision ID: 0024_remove_bootstrap_seed
Revises: 0023_managed_secrets
Create Date: 2026-07-21

`make bootstrap-admin` は volume を消して migration を再実行するため、0009/0010 が播く
JAF seed(`JAF M0` Project、`system-skills` Project、`jaf-quality-analysis` Skill 版本)が
新規配備のたび復活し、利用者が作っていない Project と DEPRECATED Skill が画面へ出ていた。
本 migration はそれらを削除する。

AGENTS の「歴史的な SkillVersion/Run snapshot は read-only audit asset として保持」を守るため、
削除はすべて条件付きとする。実 Run・Proposal・composition・Project 資産が一つでも参照している
場合は対象を残し、何も参照していない(= 事実上まっさらな配備の)場合だけ削除する。
Organization row は bootstrap_admin が要求するため削除しない。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0024_remove_bootstrap_seed"
down_revision: str | None = "0023_managed_secrets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0009 が播く JAF Skill seed の固定 ID。
_SOURCE_ID = UUID("00000000-0000-4000-8000-000000000201")
_SKILL_ID = UUID("00000000-0000-4000-8000-000000000202")
_VERSION_ID = UUID("00000000-0000-4000-8000-000000000203")
_INTERPRETATION_ID = UUID("00000000-0000-4000-8000-000000000001")

# 0010 が播く Project seed の固定 ID。
_SYSTEM_SKILL_PROJECT_ID = UUID("00000000-0000-4000-8000-000000000200")
_M0_PROJECT_ID = UUID("8f126d88-cf2d-4aa8-adf2-50b9504f6bcf")

# Project を「使用中」と判定する FK 子 table。runs だけは FK ではなく素の列で project を持つ。
_PROJECT_REFERENCE_TABLES = (
    "runs",
    "project_members",
    "secret_references",
    "managed_secret_material",
    "integrations",
    "resource_bindings",
    "project_compositions",
    "project_skill_versions",
    "effect_preauthorizations",
    "change_proposals",
)


def upgrade() -> None:
    """参照が無い場合だけ JAF Skill seed と seed Project を削除する。"""

    connection = op.get_bind()
    # Project 選好は監査資産ではなく単なる表示設定のため、削除の障害にせず先に外す。
    connection.execute(
        sa.text(
            """
            UPDATE users SET preferred_project_id = NULL
            WHERE preferred_project_id IN (
                CAST(:system_project AS uuid), CAST(:m0_project AS uuid)
            )
            """
        ),
        {"system_project": str(_SYSTEM_SKILL_PROJECT_ID), "m0_project": str(_M0_PROJECT_ID)},
    )
    _delete_skill_seed(connection)
    for project_id in (_M0_PROJECT_ID, _SYSTEM_SKILL_PROJECT_ID):
        _delete_project_if_unused(connection, project_id)


def _delete_skill_seed(connection: Connection) -> None:
    """実行履歴から参照されていない場合だけ JAF Skill seed 一式を削除する。"""

    referenced = connection.execute(
        sa.text(
            """
            SELECT
              (SELECT count(*) FROM run_skill_snapshots
                 WHERE skill_version_id = CAST(:version AS uuid))
            + (SELECT count(*) FROM change_proposals
                 WHERE skill_version_id = CAST(:version AS uuid))
            + (SELECT count(*) FROM skill_composition_items
                 WHERE skill_version_id = CAST(:version AS uuid))
            """
        ),
        {"version": str(_VERSION_ID)},
    ).scalar_one()
    if referenced:
        # 実 Run/Proposal/composition が参照する版本は audit asset として残す。
        return

    version = {"version": str(_VERSION_ID)}
    connection.execute(
        sa.text(
            "DELETE FROM project_skill_versions WHERE skill_version_id = CAST(:version AS uuid)"
        ),
        version,
    )
    connection.execute(
        sa.text("DELETE FROM runtime_manifests WHERE skill_version_id = CAST(:version AS uuid)"),
        version,
    )
    connection.execute(
        sa.text("DELETE FROM skill_versions WHERE id = CAST(:version AS uuid)"), version
    )
    # 親行は seed 以外の版本/解釈が残っていない場合だけ削除し、利用者の資産を巻き込まない。
    connection.execute(
        sa.text(
            """
            DELETE FROM skills WHERE id = CAST(:skill AS uuid)
              AND NOT EXISTS (
                SELECT 1 FROM skill_versions WHERE skill_id = CAST(:skill AS uuid)
              )
            """
        ),
        {"skill": str(_SKILL_ID)},
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM skill_interpretations WHERE id = CAST(:interpretation AS uuid)
              AND NOT EXISTS (
                SELECT 1 FROM skill_versions
                  WHERE interpretation_id = CAST(:interpretation AS uuid)
              )
              AND NOT EXISTS (
                SELECT 1 FROM runtime_manifests
                  WHERE interpretation_id = CAST(:interpretation AS uuid)
              )
            """
        ),
        {"interpretation": str(_INTERPRETATION_ID)},
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM skill_sources WHERE id = CAST(:source AS uuid)
              AND NOT EXISTS (
                SELECT 1 FROM skill_interpretations
                  WHERE skill_source_id = CAST(:source AS uuid)
              )
              AND NOT EXISTS (
                SELECT 1 FROM skill_versions WHERE skill_source_id = CAST(:source AS uuid)
              )
            """
        ),
        {"source": str(_SOURCE_ID)},
    )


def _delete_project_if_unused(connection: Connection, project_id: UUID) -> None:
    """どの資産からも参照されていない seed Project だけを削除する。"""

    clauses = " + ".join(
        f"(SELECT count(*) FROM {table} WHERE project_id = CAST(:project AS uuid))"
        for table in _PROJECT_REFERENCE_TABLES
    )
    referenced = connection.execute(
        sa.text(f"SELECT {clauses}"), {"project": str(project_id)}
    ).scalar_one()
    if referenced:
        # Run や Integration を抱えた Project は利用中とみなし、seed 由来でも残す。
        return
    connection.execute(
        sa.text("DELETE FROM projects WHERE id = CAST(:project AS uuid)"),
        {"project": str(project_id)},
    )


def downgrade() -> None:
    """Seed は復元しない。

    削除対象は schema ではなく bootstrap 時の便宜 data であり、0009/0010 の JSON manifest まで
    忠実に再構築すると checksum が不一致になり得る。既に他の Project/Skill が作られた配備へ
    seed を差し戻す価値も無いため、downgrade は明示的に何もしない。
    """
