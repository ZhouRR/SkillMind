"""原 Result 観測の専用保存と衝突監査を確認する。数値正規化や実 DB の証拠ではない。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from skillmind.agent.metering import AgentInvocation, ResultUsageObservation, UsageValue
from skillmind.core.hashing import canonical_json
from skillmind.runs import repository_budgets
from skillmind.runs.budget import BudgetConflictError, BudgetUnavailableError
from skillmind.runs.budget_store import PostgresRunBudgetStore
from skillmind.runs.domain import LeaseValidationError
from tests.runs.budget_fakes import BudgetDatabase
from tests.runs.test_budget_store import BudgetSessions
from tests.runs.test_repository_budgets import report


def raw_observation(
    db: BudgetDatabase, *, turns: object = 3, cost: object = 0.1
) -> ResultUsageObservation:
    """表示 usage や任意の final 宣言を原 Result 観測に混ぜない。"""

    return ResultUsageObservation(
        AgentInvocation.from_json(db.reservations[0].invocation_json),
        UsageValue.capture(turns),
        UsageValue.capture(cost, allow_binary64=True),
    )


def balances(db: BudgetDatabase) -> tuple[object, ...]:
    """観測だけでは動かせない元の予約・勘定・停止・最終値をまとめる。"""

    row = db.reservations[0]
    return (
        db.account.consumed_turns,
        db.account.reserved_turns,
        db.account.consumed_cost_nanos,
        db.account.reserved_cost_nanos,
        row.consumed_turns,
        row.reserved_turns,
        row.consumed_cost_nanos,
        row.reserved_cost_nanos,
        row.turns_watermark,
        row.cost_watermark,
        row.stop_confirmed_at,
        row.final_usage_at,
        row.status,
    )


@pytest.mark.parametrize(
    "turns,cost",
    [
        (None, None),
        (0, 0),
        (3, 0.1),
        (True, False),
        (-1, -0.1),
        (1.5, float("nan")),
        ("3", {"private": "body"}),
    ],
)
async def test_raw_observations_preserve_unknown_or_binary_values_without_accounting(
    turns: object, cost: object
) -> None:
    """欠測や不正値をゼロにせず、binary64 cost を正確な金額として決算しない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    observation = raw_observation(db, turns=turns, cost=cost)
    original_balances = balances(db)
    original_version = db.account.row_version
    original_row_time = db.reservations[0].updated_at
    result = await db.repository.record_observation(claim, observation)
    assert result.disposition == "OBSERVED"
    assert balances(db) == original_balances and db.account.row_version == original_version
    assert db.reservations[0].updated_at == original_row_time
    assert len(db.observations) == 1 and not db.receipts
    row = db.observations[0]
    assert row.payload_json == observation.to_json()
    assert row.observation_key == f"claude-result/{observation.invocation.invocation_id}"
    assert row.invocation_id == db.reservations[0].invocation_id
    assert row.reconcile_worker_id == claim.worker_id
    assert "private" not in str(row.payload_json)
    assert (await db.repository.record_observation(claim, observation)).disposition == "REPLAY"
    assert len(db.observations) == 1 and balances(db) == original_balances


async def test_changed_same_slot_appends_one_conflict_audit_and_keeps_original_raw_value() -> None:
    """同実行の Result 異内容を別 slot に逃がさず、停止理由と衝突監査を残す。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    first = raw_observation(db)
    second = raw_observation(db, turns=4)
    baseline = balances(db)
    await db.repository.record_observation(claim, first)
    saved = deepcopy(db.observations[0].payload_json)
    result = await db.repository.record_observation(claim, second)
    assert result.disposition == "CONFLICT" and result.account.block_code == "observation_conflict"
    assert len(db.observations) == 1 and db.observations[0].payload_json == saved
    assert len(db.receipts) == 1 and db.receipts[0].kind == "UNVERIFIABLE"
    assert db.receipts[0].disposition == "CONFLICT"
    assert db.receipts[0].payload_json["original_key"] == first.observation_key
    assert balances(db) == baseline
    assert (await db.repository.record_observation(claim, second)).disposition == "CONFLICT"
    assert len(db.receipts) == 1
    assert (await db.repository.record_observation(claim, first)).disposition == "REPLAY"
    assert db.account.block_code == "observation_conflict" and balances(db) == baseline


@pytest.mark.parametrize("damaged_value", [True, 1.0])
async def test_stored_integer_type_corruption_is_conflict_not_python_equal_replay(
    damaged_value: bool | float,
) -> None:
    """Python では等しい 1/True/1.0 も、原 JSON の型が変われば監査を上書きしない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    observation = raw_observation(db, turns=1)
    await db.repository.record_observation(claim, observation)
    row = db.observations[0]
    original_checksum = row.payload_checksum
    damaged = deepcopy(row.payload_json)
    damaged["turns"]["value"] = damaged_value
    row.payload_json = damaged
    assert row.payload_json == observation.to_json()
    saved_json = canonical_json(row.payload_json)
    assert saved_json != canonical_json(observation.to_json())
    baseline = balances(db)

    result = await db.repository.record_observation(claim, observation)

    assert result.disposition == "CONFLICT" and db.account.block_code == "observation_conflict"
    assert len(db.observations) == 1 and canonical_json(row.payload_json) == saved_json
    assert row.payload_checksum == original_checksum
    assert len(db.receipts) == 1 and db.receipts[0].kind == "UNVERIFIABLE"
    assert db.receipts[0].disposition == "CONFLICT" and balances(db) == baseline
    assert (await db.repository.record_observation(claim, observation)).disposition == "CONFLICT"
    assert len(db.receipts) == 1 and canonical_json(row.payload_json) == saved_json
    assert row.payload_checksum == original_checksum


@pytest.mark.parametrize(
    "field",
    [
        "invocation_id",
        "project_id",
        "run_id",
        "run_attempt_id",
        "user_id",
        "prompt_checksum",
        "options",
        "sdk_version",
    ],
)
async def test_raw_observation_must_match_the_complete_original_binding(field: str) -> None:
    """Run/Attempt が同じというだけでは別 options や別呼出しの Result を採用しない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    original = raw_observation(db)
    values: dict[str, Any] = {
        "invocation_id": uuid4(),
        "project_id": uuid4(),
        "run_id": uuid4(),
        "run_attempt_id": uuid4(),
        "user_id": uuid4(),
        "prompt_checksum": "3" * 64,
        "options": replace(original.invocation.options, model="different-model"),
        "sdk_version": "different-sdk",
    }
    observation = replace(
        original, invocation=replace(original.invocation, **{field: values[field]})
    )
    with pytest.raises(BudgetConflictError):
        await db.repository.record_observation(claim, observation)
    assert not db.observations and not db.receipts
    assert db.account.block_code is None


@pytest.mark.parametrize("status", ["RESERVED", "RELEASED"])
async def test_raw_observation_cannot_claim_a_result_before_original_start_intent(
    status: str,
) -> None:
    """実行記述子が保存済みでも B がなければ Result の保存を許可しない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.bind()
    claim = await db.reconciler()
    db.reservations[0].status = status
    with pytest.raises(BudgetUnavailableError):
        await db.repository.record_observation(claim, raw_observation(db))
    assert not db.observations and not db.receipts


async def test_terminal_run_and_settled_execution_still_accept_the_original_late_observation() -> (
    None
):
    """終態を根拠に旧監査を捨てず、専用核対権で原予約に保存する。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    await db.repository.record_usage(claim, report("final", 3, 30, final=True))
    await db.repository.confirm_stopped(claim, receipt_key="stop", verified_evidence="fixture/stop")
    assert db.reservations[0].status == "SETTLED"
    db.run.status = "CANCELLED"
    db.attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    before = balances(db)
    assert (
        await db.repository.record_observation(claim, raw_observation(db))
    ).disposition == "OBSERVED"
    assert balances(db) == before and db.run.status == "CANCELLED"
    with pytest.raises(LeaseValidationError):
        await db.repository.record_observation(db.claimed, raw_observation(db))  # type: ignore[arg-type]


async def test_raw_legacy_unbound_start_intent_is_not_reconstructed_from_the_report() -> None:
    """観測側の自己申告から旧実行の options を後付けしない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    observation = raw_observation(db)
    row = db.reservations[0]
    row.invocation_id = row.invocation_json = row.invocation_checksum = None
    claim = await db.reconciler()
    with pytest.raises(BudgetUnavailableError):
        await db.repository.record_observation(claim, observation)
    assert not db.observations and row.invocation_json is None


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_observation_commit_is_not_hidden_and_same_original_slot_can_be_retried(
    committed: bool,
) -> None:
    """通信断は返し、明示的な原観測の再送だけを一度の保存に収束させる。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    observation = raw_observation(db)
    original = balances(db)
    factory = BudgetSessions(db, fail_on=1, committed=committed)
    store = PostgresRunBudgetStore(factory)  # type: ignore[arg-type]
    with pytest.raises(ConnectionError):
        await store.record_observation(claim, observation)
    assert len(factory.sessions) == 1 and len(db.observations) == int(committed)
    result = await store.record_observation(claim, observation)
    assert result.disposition == ("REPLAY" if committed else "OBSERVED")
    assert len(db.observations) == 1 and balances(db) == original


class ObservationFailureSessions(BudgetSessions):
    """観測 query/flush 待機中の失効または flush 障害を注入する。"""

    def __init__(self, database: BudgetDatabase, failure: str) -> None:
        """永続状態の rollback は共通 fake に任せる。"""

        super().__init__(database)
        self.failure = failure

    def __call__(self) -> MagicMock:
        """await 中に保存済み期限を失効値へ変更し、最終 fence の拒否を再現する。"""

        session = super().__call__()

        async def flush() -> None:
            if self.failure == "flush_error":
                raise ConnectionError("flush unavailable")
            self.database.reservations[0].reconcile_expires_at = datetime.now(UTC) - timedelta(
                seconds=1
            )

        async def scalars(statement: Any) -> MagicMock:
            result = await self.database.scalars(statement)
            if statement.get_final_froms()[0].name == "run_budget_observations":
                self.database.reservations[0].reconcile_expires_at = datetime.now(UTC) - timedelta(
                    seconds=1
                )
            return result

        if self.failure == "query_expiry":
            session.scalars = AsyncMock(side_effect=scalars)
        else:
            session.flush = AsyncMock(side_effect=flush)
        return session


@pytest.mark.parametrize("failure", ["query_expiry", "flush_expiry", "flush_error"])
@pytest.mark.parametrize("existing", [False, True])
async def test_observation_deadline_or_flush_failure_rolls_back_all_new_facts(
    failure: str, existing: bool
) -> None:
    """期限後の初回・再送・衝突は commit せず、先行の正当な原観測だけ残す。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    observation = raw_observation(db)
    if existing:
        await db.repository.record_observation(claim, observation)
    original = balances(db)
    store = PostgresRunBudgetStore(ObservationFailureSessions(db, failure))  # type: ignore[arg-type]
    with pytest.raises(ConnectionError if failure == "flush_error" else LeaseValidationError):
        await store.record_observation(claim, observation)
    assert len(db.observations) == int(existing) and not db.receipts
    assert balances(db) == original and db.account.block_code is None


async def test_expiry_after_conflict_receipt_flush_rolls_back_block_and_audit_together() -> None:
    """衝突監査だけ消えて停止理由が残る、またはその逆の部分 commit を作らない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    await db.repository.record_observation(claim, raw_observation(db))
    original = balances(db)
    store = PostgresRunBudgetStore(ObservationFailureSessions(db, "flush_expiry"))  # type: ignore[arg-type]
    with pytest.raises(LeaseValidationError):
        await store.record_observation(claim, raw_observation(db, turns=4))
    assert len(db.observations) == 1 and not db.receipts
    assert db.account.block_code is None and balances(db) == original


async def test_expired_claim_cannot_reuse_its_token_after_a_new_generation() -> None:
    """同じ Worker/token を再利用しても旧期限の claim を復活させない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    old = await db.reconciler()
    db.reservations[0].reconcile_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    renewed = await db.repository.claim_reconciliation(
        project_id=old.project_id,
        run_id=old.run_id,
        execution_key="primary",
        worker_id=old.worker_id,
        token=old.token,
    )
    with pytest.raises(LeaseValidationError):
        await db.repository.record_observation(old, raw_observation(db))
    assert (
        await db.repository.record_observation(renewed, raw_observation(db))
    ).disposition == "OBSERVED"


async def test_observation_sql_follows_run_account_reservations_and_refreshes_the_slot() -> None:
    """既存 Run lock が slot を直列化し、receipt との混在や逆順 lock を追加しない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    db.session.scalars.reset_mock()
    await db.repository.record_observation(claim, raw_observation(db))
    statements = [call.args[0] for call in db.session.scalars.await_args_list]
    assert [statement.get_final_froms()[0].name for statement in statements] == [
        "runs",
        "run_budget_accounts",
        "run_budget_reservations",
        "run_budget_observations",
    ]
    assert all(
        statement.get_execution_options().get("populate_existing") is True
        for statement in statements
    )


@pytest.mark.parametrize("prior_conflict", [False, True])
@pytest.mark.parametrize("boundary", ["receipt", "flush"])
@pytest.mark.parametrize("invalid", ["worker", "token", "generation", "expiry"])
async def test_conflict_rechecks_reconciliation_generation_after_every_wait(
    monkeypatch: pytest.MonkeyPatch, prior_conflict: bool, boundary: str, invalid: str
) -> None:
    """衝突の新規保存・再送とも、最後の待機中の失効を監査の部分 commit にしない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    first = raw_observation(db)
    conflict = raw_observation(db, turns=4)
    await db.repository.record_observation(claim, first)
    if prior_conflict:
        await db.repository.record_observation(claim, conflict)
    baseline = balances(db)
    original_raw = deepcopy(db.observations[0].payload_json)
    original_block = db.account.block_code
    original_version = db.account.row_version
    factory = BudgetSessions(db)
    clock = MagicMock(wraps=datetime)
    clock.now.side_effect = lambda _: datetime.now(UTC)
    monkeypatch.setattr(repository_budgets, "datetime", clock)

    def invalidate() -> None:
        """row 世代差替えと実時刻の期限超過を区別して注入する。"""

        row = db.reservations[0]
        if invalid == "worker":
            row.reconcile_worker_id = "another-reconciler"
        elif invalid == "token":
            row.reconcile_token_hash = "0" * 64
        elif invalid == "generation":
            row.reconcile_expires_at = claim.expires_at + timedelta(seconds=30)
        else:
            clock.now.side_effect = None
            clock.now.return_value = claim.expires_at + timedelta(seconds=1)

    def session_factory() -> MagicMock:
        """実 DB の競争ではなく、指定した await 後の fence だけを検査する。"""

        session = factory()

        async def scalars(statement: Any) -> MagicMock:
            result = await db.scalars(statement)
            if statement.get_final_froms()[0].name == "run_budget_receipts":
                invalidate()
            return result

        async def flush() -> None:
            invalidate()

        if boundary == "receipt":
            session.scalars = AsyncMock(side_effect=scalars)
        else:
            session.flush = AsyncMock(side_effect=flush)
        return session

    store = PostgresRunBudgetStore(session_factory)  # type: ignore[arg-type]
    with pytest.raises(LeaseValidationError):
        await store.record_observation(claim, conflict)
    assert len(factory.sessions) == 1
    assert len(db.observations) == 1 and db.observations[0].payload_json == original_raw
    assert len(db.receipts) == int(prior_conflict)
    assert balances(db) == baseline
    assert db.account.block_code == original_block and db.account.row_version == original_version
    if boundary == "receipt":
        factory.sessions[0].flush.assert_not_awaited()


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_conflict_commit_keeps_raw_and_never_reports_unconfirmed_success(
    committed: bool,
) -> None:
    """衝突の応答喪失も隠さず、原値の明示的再送で同じ監査に収束する。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    first = raw_observation(db)
    conflicting = raw_observation(db, turns=4)
    await db.repository.record_observation(claim, first)
    baseline = balances(db)
    factory = BudgetSessions(db, fail_on=1, committed=committed)
    store = PostgresRunBudgetStore(factory)  # type: ignore[arg-type]
    with pytest.raises(ConnectionError):
        await store.record_observation(claim, conflicting)
    assert len(factory.sessions) == 1
    assert len(db.receipts) == int(committed)
    assert db.account.block_code == ("observation_conflict" if committed else None)
    assert len(db.observations) == 1 and db.observations[0].payload_json == first.to_json()
    result = await store.record_observation(claim, conflicting)
    assert result.disposition == "CONFLICT" and db.account.block_code == "observation_conflict"
    assert len(db.receipts) == 1 and balances(db) == baseline
