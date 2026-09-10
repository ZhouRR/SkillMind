"""0027 の共通 SQL 条件をメモリ内で実行し、監査を消さない downgrade を検証する。

SQLite は PostgreSQL の構文、lock、DO block、DDL transaction の証明ではない。
外部 DB に接続せず、NULL-safe 比較の表記だけ変えた SELECT と Alembic 呼出順を検査する。
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, call
from uuid import UUID

import pytest

from skillmind.db.models import AgentSession

SessionAudit = tuple[str | None, ...]
_LOCK = "LOCK TABLE agent_sessions IN ACCESS EXCLUSIVE MODE"


def _session(
    identifier: int = 1,
    *,
    run: int = 1,
    attempt: int | None = None,
    sdk: bool = True,
    kind: str | None = "PRIMARY",
    mode: str = "INITIAL",
    status: str = "CLOSED",
    parent: int | None = None,
) -> SessionAudit:
    """旧制約との互換性だけを検査する、実環境と無関係の Session 行を作る。"""

    return (
        str(UUID(int=identifier)),
        str(UUID(int=run)),
        str(UUID(int=identifier if attempt is None else attempt)),
        str(UUID(int=100 + identifier)) if sdk else None,
        kind,
        mode,
        status,
        str(UUID(int=parent)) if parent is not None else None,
        "sha256:" + "a" * 64 if parent is not None else None,
    )


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """対象 migration だけを読み、Alembic の全 operation を隔離する。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions/0027_subagent_sessions.py"
    spec = importlib.util.spec_from_file_location("subagent_session_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "op", Mock())
    return module


@pytest.mark.parametrize(
    ("rows", "compatible"),
    [
        pytest.param([], True, id="empty"),
        pytest.param([_session(status="ACTIVE")], True, id="one-active-primary"),
        pytest.param(
            [
                _session(),
                _session(2, mode="RESUME", parent=1),
                _session(3, mode="FORK", parent=2),
                _session(4, mode="REPLACE", parent=3, status="ACTIVE"),
            ],
            True,
            id="sequential-primary-keeps-parent-and-checkpoint",
        ),
        pytest.param(
            [_session(status="ACTIVE"), _session(2, run=2, status="ACTIVE")],
            True,
            id="active-primary-in-independent-runs",
        ),
        *[
            pytest.param(
                [_session(kind="SUBAGENT", mode="BRANCH", status=status, sdk=sdk)],
                False,
                id=f"child-{status.lower()}-{'with' if sdk else 'without'}-sdk",
            )
            for status in ("ACTIVE", "CLOSED", "FAILED", "INTERRUPTED")
            for sdk in (True, False)
        ],
        pytest.param([_session(sdk=False)], False, id="primary-without-sdk"),
        pytest.param([_session(kind=None)], False, id="null-kind-is-not-primary"),
        pytest.param([_session(kind="UNKNOWN")], False, id="unknown-kind"),
        pytest.param([_session(mode="BRANCH")], False, id="primary-labelled-branch"),
        pytest.param(
            [_session(), _session(2, attempt=1)], False, id="duplicate-closed-attempt"
        ),
        pytest.param(
            [_session(status="ACTIVE"), _session(2, status="ACTIVE")],
            False,
            id="duplicate-active-run-across-attempts",
        ),
        pytest.param(
            [_session(), _session(2, attempt=1, kind="SUBAGENT", mode="BRANCH")],
            False,
            id="closed-primary-and-child-share-attempt",
        ),
    ],
)
def test_downgrade_evaluates_original_guard_without_changing_audit(
    migration: ModuleType, rows: list[SessionAudit], compatible: bool,
) -> None:
    """実際の DO 条件を共通 SQL で評価し、拒否後の DDL と監査の清掃を許さない。"""

    with closing(sqlite3.connect(":memory:")) as database:
        # 本来 NOT NULL の kind も緩め、壊れた行の UNKNOWN が IF を通過しないことを検査する。
        database.execute(
            "CREATE TABLE agent_sessions (id TEXT, run_id TEXT, run_attempt_id TEXT, "
            "sdk_session_id TEXT, session_kind TEXT, continuation_mode TEXT, status TEXT, "
            "parent_session_id TEXT, checkpoint_checksum TEXT)"
        )
        database.executemany("INSERT INTO agent_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        before = database.execute("SELECT * FROM agent_sessions ORDER BY id").fetchall()

        def execute(statement: str) -> None:
            """lock は記録だけにし、migration が送った guard 自体をメモリ内で評価する。"""

            if statement == _LOCK:
                return
            match = re.fullmatch(
                r"\s*DO \$\$\s*BEGIN\s*IF\s+(.*?)\s+THEN\s+"
                r"RAISE EXCEPTION\s+'Cannot downgrade 0027: "
                r"AgentSession audit is incompatible with 0026';\s*END IF;\s*END \$\$;\s*",
                statement,
                flags=re.DOTALL,
            )
            assert match is not None, "Unexpected SQL or audit-changing statement"
            # 古い SQLite は IS DISTINCT FROM を解釈しないため、同じ NULL-safe の
            # IS NOT 表記だけに置換する。NULL と未知 kind を含む行で実際に条件を評価する。
            predicate = match.group(1)
            comparison = "session_kind IS DISTINCT FROM 'PRIMARY'"
            assert predicate.count(comparison) == 1
            predicate = predicate.replace(comparison, "session_kind IS NOT 'PRIMARY'")
            result = database.execute("SELECT " + predicate).fetchone()
            assert result is not None and result[0] in (0, 1)
            if result[0]:
                raise RuntimeError("incompatible session audit")

        migration.op.execute.side_effect = execute
        if compatible:
            migration.downgrade()
            assert [operation[0] for operation in migration.op.method_calls] == [
                "execute", "execute", "drop_constraint", "drop_constraint", "alter_column",
                "drop_constraint", "drop_index", "drop_index", "create_index", "drop_index",
                "create_unique_constraint",
            ]
            migration.op.alter_column.assert_called_once_with(
                "agent_sessions", "sdk_session_id", nullable=False
            )
            migration.op.create_unique_constraint.assert_called_once_with(
                "uq_agent_sessions_run_attempt", "agent_sessions", ["run_attempt_id"]
            )
            index = migration.op.create_index.call_args
            assert index.args == ("uq_agent_sessions_active_run", "agent_sessions", ["run_id"])
            assert index.kwargs["unique"] is True
            assert str(index.kwargs["postgresql_where"]) == "status = 'ACTIVE'"
        else:
            with pytest.raises(RuntimeError, match="incompatible session audit"):
                migration.downgrade()
            assert [operation[0] for operation in migration.op.method_calls] == [
                "execute", "execute",
            ]
        assert migration.op.method_calls[0] == call.execute(_LOCK)
        assert len(migration.op.execute.call_args_list) == 2
        assert database.execute("SELECT * FROM agent_sessions ORDER BY id").fetchall() == before


def test_downgrade_lock_failure_prevents_guard_and_every_ddl(migration: ModuleType) -> None:
    """lock を取得できなければ、検査にも制約の変更にも進まない。"""

    migration.op.execute.side_effect = RuntimeError("lock unavailable")
    with pytest.raises(RuntimeError, match="lock unavailable"):
        migration.downgrade()
    assert migration.op.method_calls == [call.execute(_LOCK)]


def test_upgrade_keeps_existing_partial_indexes_and_session_checks(migration: ModuleType) -> None:
    """既存の upgrade は子監査を保存できるままとし、新たな清掃や backfill を足さない。"""

    migration.upgrade()
    assert migration.revision == "0027_subagent_sessions"
    assert migration.down_revision == "0026_frontend_module_versions"
    assert [operation[0] for operation in migration.op.method_calls] == [
        "drop_constraint", "create_index", "drop_index", "create_index", "create_index",
        "create_check_constraint", "alter_column", "create_check_constraint",
        "create_check_constraint",
    ]
    migration.op.drop_constraint.assert_called_once_with(
        "uq_agent_sessions_run_attempt", "agent_sessions", type_="unique"
    )
    indexes = migration.op.create_index.call_args_list
    assert indexes[0].args == (
        "uq_agent_sessions_primary_run_attempt", "agent_sessions", ["run_attempt_id"],
    )
    assert indexes[0].kwargs["unique"] is True
    assert str(indexes[0].kwargs["postgresql_where"]) == "session_kind = 'PRIMARY'"
    assert indexes[1].args == (
        "uq_agent_sessions_active_run", "agent_sessions", ["run_id"],
    )
    assert indexes[1].kwargs["unique"] is True
    assert str(indexes[1].kwargs["postgresql_where"]) == (
        "status = 'ACTIVE' AND session_kind = 'PRIMARY'"
    )
    assert indexes[2] == call(
        "ix_agent_sessions_parent_session_id_kind", "agent_sessions",
        ["parent_session_id", "session_kind"], unique=False,
    )
    migration.op.alter_column.assert_called_once_with(
        "agent_sessions", "sdk_session_id", nullable=True
    )
    assert migration.op.create_check_constraint.call_args_list == [
        call(
            "ck_agent_sessions_session_kind", "agent_sessions",
            "session_kind IN ('PRIMARY', 'SUBAGENT')",
        ),
        call(
            "ck_agent_sessions_primary_has_sdk_session", "agent_sessions",
            "sdk_session_id IS NOT NULL OR session_kind = 'SUBAGENT'",
        ),
        call(
            "ck_agent_sessions_branch_is_subagent", "agent_sessions",
            "(continuation_mode = 'BRANCH') = (session_kind = 'SUBAGENT')",
        ),
    ]
    model_indexes = {
        index.name: index for index in AgentSession.metadata.tables["agent_sessions"].indexes
    }
    for index in indexes[:2]:
        model_index = model_indexes[index.args[0]]
        assert model_index.unique is True
        assert str(model_index.dialect_options["postgresql"]["where"]) == str(
            index.kwargs["postgresql_where"]
        )
    assert AgentSession.__table__.columns["sdk_session_id"].nullable is True
