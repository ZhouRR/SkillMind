"""Alembic 設定へ database URL を渡す境界を検証する。"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from projectmind.db.alembic import escape_config_value
from projectmind.db.base import Base


def test_percent_encoded_password_survives_alembic_config() -> None:
    """URL encode 済み password が ConfigParser の interpolation で失敗しないことを確認する。"""

    database_url = "postgresql+asyncpg://projectmind:P%40ssword@postgres:5432/projectmind"
    config = Config()
    config.set_main_option("sqlalchemy.url", escape_config_value(database_url))

    assert config.get_main_option("sqlalchemy.url") == database_url


def test_run_skill_seed_json_uses_driver_sql_without_bind_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0009 の固定 JSON の colon が bind parameter 化されないことを確認する。"""

    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0009_run_skill_snapshots.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0009_run_skill_snapshots", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    statements: list[str] = []
    execute_literal_sql = migration._execute_literal_sql
    monkeypatch.setattr(migration, "_execute_literal_sql", statements.append)
    migration._insert_seed_rows(datetime(2026, 7, 3, tzinfo=UTC))

    manifest_statement = next(
        statement for statement in statements if "INSERT INTO skill_interpretations" in statement
    )
    assert '"confidence":1' in manifest_statement
    assert '"required":true' in manifest_statement

    connection = Mock()
    monkeypatch.setattr(migration.op, "get_bind", Mock(return_value=connection))
    execute_literal_sql(manifest_statement)
    connection.exec_driver_sql.assert_called_once_with(manifest_statement)


def test_migration_chain_has_a_single_expected_head() -> None:
    """最新 migration が Alembic の唯一の head であることを確認する。

    head を明示で釘付けにするのは、分岐した revision が混入したときに `upgrade head` が
    曖昧になり、配備先ごとに適用結果が変わるのを防ぐため。migration を追加したら本 assertion も
    更新する。
    """

    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["0034_project_row_version"]


def test_revision_ids_fit_default_alembic_version_column() -> None:
    """Alembic 既定の varchar(32) に収まる revision ID だけを許可する。"""

    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    scripts = ScriptDirectory.from_config(config)

    oversized_revisions = [
        revision.revision for revision in scripts.walk_revisions() if len(revision.revision) > 32
    ]

    assert oversized_revisions == []


def test_session_store_models_are_registered_in_metadata() -> None:
    """Migration と application model が同じ二つの SessionStore table を共有する。"""

    assert "agent_session_transcripts" in Base.metadata.tables
    assert "agent_session_entries" in Base.metadata.tables


def test_tool_audit_models_are_registered_in_metadata() -> None:
    """Migration と application model が Tool audit の三 table を共有する。"""

    assert "tool_calls" in Base.metadata.tables
    assert "permission_decisions" in Base.metadata.tables
    assert "evidence" in Base.metadata.tables


def test_agent_result_models_are_registered_in_metadata() -> None:
    """AgentSession と immutable RunResult が metadata に登録されることを確認する。"""

    assert "agent_sessions" in Base.metadata.tables
    assert "run_results" in Base.metadata.tables
    assert "agent_session_id" in Base.metadata.tables["run_events"].columns
    assert {
        "result_kind",
        "evidence_refs_json",
        "artifact_refs_json",
        "change_proposal_refs_json",
        "optional_schema_identity_json",
    }.issubset(Base.metadata.tables["run_results"].columns.keys())


def test_controlled_effect_models_are_registered_in_metadata() -> None:
    """S2/S5 の Integration、binding、Proposal、Approval、Effect table を固定する。"""

    assert {
        "secret_references",
        "integrations",
        "resource_bindings",
        "effect_preauthorizations",
        "change_proposals",
        "change_approvals",
        "effect_executions",
    }.issubset(Base.metadata.tables)
    assert "change_proposal_id" in Base.metadata.tables["user_interactions"].columns


def test_managed_secret_material_is_registered_in_metadata() -> None:
    """MANAGED 密文 table が公開 read model と分離して metadata に登録される。"""

    table = Base.metadata.tables["managed_secret_material"]
    assert {"kek_version", "nonce", "ciphertext"}.issubset(table.columns.keys())
    assert {foreign_key.target_fullname for foreign_key in table.foreign_keys} == {
        "secret_references.id",
        "projects.id",
    }


def test_skill_import_models_are_registered_in_metadata() -> None:
    """SkillSource と SkillInterpretation が migration metadata に登録される。"""

    assert "skill_sources" in Base.metadata.tables
    assert "skill_interpretations" in Base.metadata.tables


def test_skill_source_file_index_is_registered_in_metadata() -> None:
    """Object storage source を再構築する immutable file index 列を固定する。"""

    sources = Base.metadata.tables["skill_sources"]
    assert "source_file_index_json" in sources.columns
    assert sources.columns["source_file_index_json"].nullable is False


def test_skill_interpretation_execution_columns_are_registered() -> None:
    """Model interpretation 用の report/execution 列が metadata に登録される。"""

    columns = Base.metadata.tables["skill_interpretations"].columns
    assert "report_json" in columns
    assert "execution_json" in columns
    assert columns["report_json"].nullable is True
    assert columns["execution_json"].nullable is True


def test_skill_interpretation_lineage_columns_are_registered() -> None:
    """Reinterpretation lineage の parent/adjustment 列が metadata に登録される。"""

    table = Base.metadata.tables["skill_interpretations"]
    assert "parent_interpretation_id" in table.columns
    assert "adjustment_json" in table.columns
    assert table.columns["parent_interpretation_id"].nullable is True
    assert {foreign_key.target_fullname for foreign_key in table.foreign_keys} == {
        "skill_sources.id",
        "skill_interpretations.id",
    }


def test_evaluation_model_is_registered_in_metadata() -> None:
    """Evaluation table と Result foreign key が application metadata に登録される。"""

    table = Base.metadata.tables["evaluations"]
    assert {foreign_key.target_fullname for foreign_key in table.foreign_keys} == {"run_results.id"}


def test_skill_version_models_are_registered_in_metadata() -> None:
    """Skill、Version、frozen Manifest table が application metadata に登録される。"""

    assert "skills" in Base.metadata.tables
    assert "skill_versions" in Base.metadata.tables
    assert "runtime_manifests" in Base.metadata.tables
    assert "run_skill_snapshots" in Base.metadata.tables


def test_skill_library_scope_models_are_registered_in_metadata() -> None:
    """Organization 資産と Project 明示有効化の収縮後 schema を metadata で固定する。"""

    sources = Base.metadata.tables["skill_sources"]
    skills = Base.metadata.tables["skills"]
    enablements = Base.metadata.tables["project_skill_versions"]

    assert "organization_id" in sources.columns
    assert "project_id" not in sources.columns
    assert "organization_id" in skills.columns
    assert {foreign_key.target_fullname for foreign_key in enablements.foreign_keys} == {
        "projects.id",
        "skill_versions.id",
    }


def test_skill_composition_models_are_registered_in_metadata() -> None:
    """組合・束縛・Project 有効化の三 table と主要制約が metadata に登録される。"""

    assert "skill_compositions" in Base.metadata.tables
    items = Base.metadata.tables["skill_composition_items"]
    assert {foreign_key.target_fullname for foreign_key in items.foreign_keys} == {
        "skill_compositions.id",
        "skill_versions.id",
    }
    enablements = Base.metadata.tables["project_compositions"]
    assert {foreign_key.target_fullname for foreign_key in enablements.foreign_keys} == {
        "projects.id",
        "skill_compositions.id",
    }


def test_identity_and_project_models_are_registered_in_metadata() -> None:
    """認証、Project、membership table が migration metadata に登録される。"""

    assert {
        "organizations", "users", "auth_sessions", "projects", "project_members",
        "project_member_events",
    } <= set(Base.metadata.tables)
    assert "preferred_project_id" in Base.metadata.tables["users"].columns


def test_frontend_module_version_model_is_registered_in_metadata() -> None:
    """生成 module 版の凍結列が metadata に登録される (計画 §24 M1)。

    配信時 CSP を版と一緒に凍結するのが D2 の前提。列が消えると header を版から復元できず、
    「隔離は配信側の設定次第」という状態へ静かに戻る。
    """

    table = Base.metadata.tables["frontend_module_versions"]
    assert {
        "module_api_version",
        "source_hash",
        "lockfile_hash",
        "bundle_hash",
        "content_security_policy",
        "static_report_json",
        "build_report_json",
    }.issubset(table.columns.keys())
    assert {foreign_key.target_fullname for foreign_key in table.foreign_keys} == {
        "skill_versions.id",
        "users.id",
    }
