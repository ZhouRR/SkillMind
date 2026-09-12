"""DB write の固定内容・許可範囲・一行条件と、回执 transaction の制御を検証する。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from skillmind.effects.database_write import (
    build_database_write,
    database_row_revision,
    validate_database_write_proposal,
)
from skillmind.effects.postgres_write import (
    DatabaseWriteConflictError,
    DatabaseWriteUncertainError,
    PostgresDatabaseWriteSource,
    apply_database_write,
    lookup_database_write,
)
from skillmind.effects.proposal import parse_change_proposal_request


def command(**overrides):
    """業務 Skill に依存しない合成 table と許可列で原要求を作る。"""

    arguments = {
        "effect_id": uuid4(),
        "project_id": uuid4(),
        "run_id": uuid4(),
        "integration_id": uuid4(),
        "table": "example.reviews",
        "operation": "INSERT",
        "key": {"id": "record-1"},
        "values": {"status": "RUNNING"},
        "expected": None,
        "scope": {
            "tables": ["example.reviews"],
            "operations": ["INSERT", "UPDATE"],
            "write_columns": [
                "example.reviews.id",
                "example.reviews.status",
                "example.reviews.result",
            ],
        },
    }
    return build_database_write(**{**arguments, **overrides})


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE"])
def test_public_proposal_parser_preserves_database_row_and_checkpoint(operation: str) -> None:
    """公開の提案入口から DB validator まで、主キー・原行・checkpoint を受け渡す。"""

    example = (
        Path(__file__).resolve().parents[3] / "contracts/examples/change-propose-request.v1.json"
    )
    request = json.loads(example.read_text())
    expected = None if operation == "INSERT" else {"id": "record-1", "status": "RUNNING"}
    original = command(operation=operation, expected=expected)
    request.update(
        effect_intent_key="save_record",
        resource_key="records",
        capability_version="database.write/v1",
        operation=operation,
        target={"locator": original.table, "display": "Reviewed record"},
        changes=[
            {
                "path": "/row",
                "action": "SET",
                "value": {"key": original.key, "values": original.values, "expected": expected},
            }
        ],
        precondition={"revision": database_row_revision(expected)},
        verification={"method": "READ_BACK", "paths": ["/row"]},
    )
    draft = parse_change_proposal_request(request)
    payload = validate_database_write_proposal(
        draft,
        binding_scope={
            "tables": [original.table],
            "operations": [operation],
            "write_columns": [f"{original.table}.id", f"{original.table}.status"],
        },
    )
    assert payload["expected"] == expected
    assert payload["key"] == original.key
    assert draft.checkpoint == request["checkpoint"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"table": "example.reviews; DROP TABLE other"},
        {"table": "example.reviews WHERE true"},
        {"table": "skillmind_effects.execution_receipts"},
        {"table": "other.reviews"},
        {"operation": "DELETE"},
        {"key": {}},
        {"key": {"id": None}},
        {"key": {"id": [1, 2]}},
        {"values": {"id": "replacement"}},
        {"values": {"unapproved": "value"}},
        {"values": {"status; DROP TABLE other": "value"}},
        {"values": {"result": float("nan")}},
        {"values": {"status": "x" * 1_048_576}},
        {"operation": "UPDATE", "expected": None},
        {"expected": {"id": "record-1"}},
        {
            "scope": {
                "tables": ["example.reviews"],
                "operations": ["INSERT"],
                "write_columns": ["example.reviews.status"],
            }
        },
    ],
)
def test_invalid_target_scope_and_values_are_rejected(overrides) -> None:
    """接続前に構造化要求を検証し、SQL/主キー変更/列範囲逸脱を受理しない。"""

    with pytest.raises(ValueError):
        command(**overrides)


def test_command_freezes_nested_values_and_binds_all_execution_identities() -> None:
    """受付後の元 object 変更も取得 property の変更も原 checksum を変えない。"""

    values = {"result": {"findings": ["first"]}}
    original = command(values=values)
    values["result"]["findings"].append("later")
    copy = original.values
    copy["result"]["findings"].append("changed")
    assert original.values == {"result": {"findings": ["first"]}}
    for name in ("effect_id", "project_id", "run_id", "integration_id"):
        fields = {
            field: getattr(original, field)
            for field in ("effect_id", "project_id", "run_id", "integration_id")
        }
        fields[name] = uuid4()
        assert command(values=original.values, **fields).checksum != original.checksum


def rows(value=None, *, all_rows=None, rowcount=1):
    """SQLAlchemy result の観測値だけを返し、実 PostgreSQL と混同しない。"""

    result = MagicMock()
    result.rowcount = rowcount
    result.mappings.return_value.one_or_none.return_value = value
    result.mappings.return_value.one.return_value = value
    result.mappings.return_value.__iter__.return_value = iter(all_rows or [])
    return result


class Connection:
    """SQL の送信順と commit/rollback 制御だけを観測する合成接続。"""

    def __init__(self, responses, *, commit_error: bool = False) -> None:
        """各 SQL の返却値と commit 応答欠落を注入する。"""

        self.execute = AsyncMock(side_effect=responses)
        self.exec_driver_sql = AsyncMock()
        self.events: list[str] = []
        self.commit_error = commit_error

    @asynccontextmanager
    async def begin(self):
        """例外による rollback と commit 応答不明を異なる経路で観測する。"""

        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        self.events.append("commit")
        if self.commit_error:
            raise OperationalError("private SQL", {}, RuntimeError("private endpoint"))


def insert_responses(*, before=None, after=None, primary_key=True, generated=""):
    """INSERT と回読の合成 SQL 結果を作る。制約・lock の実動作は証明しない。"""

    return [
        rows(),
        rows(),
        rows(
            all_rows=[
                {"name": "id", "primary_key": primary_key, "generated": "", "identity": ""},
                {"name": "status", "primary_key": False, "generated": generated, "identity": ""},
            ]
        ),
        rows(before),
        rows({"payload": '{"id":"record-1","status":"RUNNING"}'}),
        rows(),
        rows(after or {"payload": '{"id":"record-1","status":"RUNNING"}'}),
        rows(),
    ]


async def test_insert_and_receipt_share_transaction_and_revalidate_authority() -> None:
    """原状態確認・変更・回执保存が一つの transaction 内で行われる制御を検証する。"""

    db = Connection(insert_responses())
    authorize = AsyncMock()
    result = await apply_database_write(db, command(), authorize=authorize)
    assert result.before is None
    assert result.after == {"id": "record-1", "status": "RUNNING"}
    assert result.replayed is False
    assert db.events == ["begin", "commit"]
    assert authorize.await_count == 3
    sql = [str(call.args[0]) for call in db.execute.await_args_list]
    assert sql[0] == "SELECT pg_advisory_xact_lock(:key)"
    assert sql[-1].startswith('INSERT INTO "skillmind_effects"."execution_receipts"')
    assert "record-1" not in "\n".join(sql)


async def test_original_receipt_proves_replay_without_touching_current_business_row() -> None:
    """原 Effect/checksum の回执だけを成功根拠とし、同じ内容の新規行を根拠にしない。"""

    original = command()
    receipt = {
        "request_checksum": original.checksum,
        "before": "null",
        "after": '{"id":"record-1","status":"RUNNING"}',
    }
    db = Connection([rows(), rows(receipt)])
    result = await apply_database_write(db, original, authorize=AsyncMock())
    assert result.replayed
    assert db.execute.await_count == 2
    assert all(
        '"example"."reviews"' not in str(call.args[0]) for call in db.execute.await_args_list
    )


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE"])
async def test_only_update_locks_the_original_row(operation):
    """INSERT 専用 role を維持しつつ、UPDATE の原行照合と回読は lock を失わない。"""

    expected = {"id": "record-1", "status": "PENDING"} if operation == "UPDATE" else None
    db = Connection(insert_responses(
        before={"payload": json.dumps(expected)} if expected is not None else None
    ))
    receipt = await apply_database_write(
        db, command(operation=operation, expected=expected), authorize=AsyncMock()
    )
    assert receipt.before == expected
    row_queries = [
        str(call.args[0]) for call in db.execute.await_args_list
        if 'FROM "example"."reviews" t WHERE' in str(call.args[0])
    ]
    assert len(row_queries) == 2
    assert all(sql.endswith(" FOR UPDATE") == (operation == "UPDATE") for sql in row_queries)
    assert db.events == ["begin", "commit"]


async def test_same_row_values_without_original_receipt_are_a_conflict() -> None:
    """他の実行が同じ値を保存していても原 INSERT の成功として採用しない。"""

    db = Connection(insert_responses(before={"payload": '{"id":"record-1","status":"RUNNING"}'}))
    with pytest.raises(DatabaseWriteConflictError):
        await apply_database_write(db, command(), authorize=AsyncMock())
    assert db.events == ["begin", "rollback"]
    assert db.execute.await_count == 4


@pytest.mark.parametrize(
    "before,after",
    [
        ('{"id":"record-1"}', '{"id":"record-1","status":"RUNNING"}'),
        ("null", "{}"),
        ("null", '{"id":"record-1"}'),
    ],
)
async def test_matching_checksum_does_not_hide_incomplete_receipt_facts(before, after) -> None:
    """回执本文が壊れていれば checksum 列だけを根拠に原成功を返さない。"""

    original = command()
    db = Connection(
        [
            rows(
                {
                    "request_checksum": original.checksum,
                    "before": before,
                    "after": after,
                }
            )
        ]
    )
    with pytest.raises(DatabaseWriteConflictError, match="original facts"):
        await lookup_database_write(db, original)


@pytest.mark.parametrize("options", [{"primary_key": False}, {"generated": "s"}])
async def test_database_metadata_rejects_non_key_or_generated_columns(options) -> None:
    """table の実メタデータが提案を許さない場合、変更 SQL を発行しない。"""

    db = Connection(insert_responses(**options))
    with pytest.raises(DatabaseWriteConflictError):
        await apply_database_write(db, command(), authorize=AsyncMock())
    assert db.execute.await_count == 3


async def test_authority_loss_after_mutation_rolls_back_instead_of_committing() -> None:
    """回读後に実行権を失えば業務変更と回执をともに commit しない。"""

    db = Connection(insert_responses())
    authorize = AsyncMock(side_effect=[None, None, PermissionError("revoked")])
    with pytest.raises(PermissionError):
        await apply_database_write(db, command(), authorize=authorize)
    assert db.events == ["begin", "rollback"]


async def test_commit_error_stays_uncertain_and_does_not_retry() -> None:
    """commit 応答欠落で rollback 成功や未実行を主張せず、原回执の確認へ閉じる。"""

    db = Connection(insert_responses(), commit_error=True)
    with pytest.raises(DatabaseWriteUncertainError, match="original database effect") as caught:
        await apply_database_write(db, command(), authorize=AsyncMock())
    assert "private" not in str(caught.value)
    assert db.events == ["begin", "commit"]
    assert db.execute.await_count == 8


async def test_lookup_is_read_only_and_absence_is_only_an_observation() -> None:
    """照会は回执 SELECT 一回だけで、未検出でも業務 INSERT を補わない。"""

    db = Connection([rows()])
    assert await lookup_database_write(db, command()) is None
    assert db.exec_driver_sql.await_args_list[0].args == ("SET TRANSACTION READ ONLY",)
    assert db.execute.await_count == 1


async def test_tampered_command_fails_before_opening_transaction() -> None:
    """保存した checksum と異なる主キー・値は SQL 接続処理前に拒否する。"""

    original = command()
    db = Connection([])
    with pytest.raises(ValueError, match="checksum"):
        await apply_database_write(
            db, replace(original, values_json='{"status":"DONE"}'), authorize=AsyncMock()
        )
    assert db.events == []


@pytest.mark.parametrize("lookup", [False, True])
async def test_source_closes_dedicated_connection_and_preserves_read_only_lookup(
    monkeypatch: pytest.MonkeyPatch,
    lookup: bool,
) -> None:
    """実行と照会を別接続にし、照会のために write transaction を開かない。"""

    original = command()
    db = Connection([rows()] if lookup else insert_responses())
    engine = AsyncMock()
    engine.connect = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=db)))
    factory = MagicMock(return_value=engine)
    monkeypatch.setattr("skillmind.effects.postgres_write.create_database_engine", factory)
    source = PostgresDatabaseWriteSource()
    method = source.lookup if lookup else source.apply
    result = await method({}, "fixture-only", original, authorize=AsyncMock())
    assert (result is None) == lookup
    assert factory.call_args.kwargs["read_only"] == lookup
    engine.dispose.assert_awaited_once()


async def test_source_authorization_failure_does_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接続前に失権が判明した要求は DB に触れない。"""

    factory = MagicMock()
    monkeypatch.setattr("skillmind.effects.postgres_write.create_database_engine", factory)
    with pytest.raises(PermissionError):
        await PostgresDatabaseWriteSource().apply(
            {}, "fixture-only", command(), authorize=AsyncMock(side_effect=PermissionError())
        )
    factory.assert_not_called()
