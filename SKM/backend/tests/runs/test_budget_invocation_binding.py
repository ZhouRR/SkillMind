"""原 invocation の固定と B の明示照合を fake transaction で検証する。実 DB ではない。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from skillmind.agent.metering import AgentInvocation, UsageValue
from skillmind.runs.budget import (
    BudgetConflictError,
    BudgetError,
    BudgetInvocationBinding,
    BudgetReservationRequest,
    BudgetUnavailableError,
)
from skillmind.runs.budget_store import PostgresRunBudgetStore
from skillmind.runs.domain import LeaseValidationError, RunCancellationRequestedError
from tests.runs.budget_fakes import (
    START_OWNER_TOKEN,
    BudgetDatabase,
    budget_invocation,
    start_arguments,
)
from tests.runs.test_budget_store import BudgetSessions


async def test_binding_is_immutable_metadata_and_preserves_all_previous_hashes() -> None:
    """小さい実 turns と binary64 cost を保存しても預留や信用済み計量は変わらない。"""

    db = BudgetDatabase()
    await db.reserve()
    row = db.reservations[0]
    old_hashes = (row.group_checksum, row.request_checksum, db.account.policy_checksum)
    old_account = deepcopy(db.account.__dict__)
    invocation = budget_invocation(db.claimed, turns=4)
    invocation = replace(
        invocation,
        options=replace(
            invocation.options, max_budget_usd=UsageValue.capture(0.1, allow_binary64=True)
        ),
    )
    binding = await db.repository.bind_invocation(
        db.claimed,
        execution_key="primary",
        invocation=invocation,
        start_owner_token=START_OWNER_TOKEN,
    )
    assert binding == BudgetInvocationBinding(row.id, invocation.invocation_id, invocation.checksum)
    assert row.invocation_json is not None
    assert AgentInvocation.from_json(row.invocation_json) == invocation
    assert row.invocation_json["options"]["max_budget_usd"] == {
        "kind": "BINARY64",
        "value": (0.1).hex(),
    }
    saved_at = row.updated_at
    assert (
        await db.repository.bind_invocation(
            db.claimed,
            execution_key="primary",
            invocation=invocation,
            start_owner_token=START_OWNER_TOKEN,
        )
        == binding
    )
    assert row.updated_at == saved_at
    assert (row.group_checksum, row.request_checksum, db.account.policy_checksum) == old_hashes
    assert {
        key: value for key, value in db.account.__dict__.items() if key != "_sa_instance_state"
    } == {key: value for key, value in old_account.items() if key != "_sa_instance_state"}
    assert row.status == "RESERVED" and row.reserved_turns == 8 and row.consumed_turns == 0
    assert row.stop_confirmed_at is None and row.final_usage_at is None
    assert not db.receipts and not db.observations


@pytest.mark.parametrize(
    "field", ["invocation_id", "prompt_checksum", "options", "sdk_version", "cli_version"]
)
async def test_rebinding_any_execution_content_cannot_replace_the_original(field: str) -> None:
    """同じ execution key を新しい ID や options の抜け道にしない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.bind()
    row = db.reservations[0]
    original = AgentInvocation.from_json(row.invocation_json)
    values: dict[str, Any] = {
        "invocation_id": uuid4(),
        "prompt_checksum": "3" * 64,
        "options": replace(original.options, max_turns=3),
        "sdk_version": "other-sdk",
        "cli_version": "other-cli",
    }
    with pytest.raises(BudgetConflictError):
        await db.repository.bind_invocation(
            db.claimed,
            execution_key="primary",
            invocation=replace(original, **{field: values[field]}),
            start_owner_token=START_OWNER_TOKEN,
        )
    assert (
        row.invocation_json == original.to_json() and row.invocation_checksum == original.checksum
    )


@pytest.mark.parametrize(
    "field", ["project_id", "run_id", "run_attempt_id", "user_id", "run_actor"]
)
async def test_binding_rejects_foreign_scope_including_the_locked_original_actor(
    field: str,
) -> None:
    """claim だけ正しくても Run の原 actor が一致しなければ固定しない。"""

    db = BudgetDatabase()
    await db.reserve()
    invocation = budget_invocation(db.claimed)
    if field == "run_actor":
        db.run.permission_snapshot_json = {"actor_id": str(uuid4())}
    else:
        changes: dict[str, Any] = {field: uuid4()}
        invocation = replace(invocation, **changes)
    with pytest.raises(BudgetConflictError):
        await db.repository.bind_invocation(
            db.claimed,
            execution_key="primary",
            invocation=invocation,
            start_owner_token=START_OWNER_TOKEN,
        )
    assert db.reservations[0].invocation_json is None


async def test_binding_rejects_increased_turn_limit_and_same_invocation_on_two_children() -> None:
    """親子は同じ Attempt を共有しても、一呼出しを二つの予約へ流用できない。"""

    db = BudgetDatabase()
    await db.reserve()
    with pytest.raises(BudgetConflictError, match="turn limit"):
        await db.repository.bind_invocation(
            db.claimed,
            execution_key="primary",
            invocation=budget_invocation(db.claimed, turns=9),
            start_owner_token=START_OWNER_TOKEN,
        )
    await db.start()
    await db.repository.reserve_group(
        db.claimed,
        group_key="children",
        requests=(
            BudgetReservationRequest("child/a", 2, 20, "primary"),
            BudgetReservationRequest("child/b", 2, 20, "primary"),
        ),
    )
    invocation = budget_invocation(db.claimed, turns=2)
    await db.repository.bind_invocation(
        db.claimed,
        execution_key="child/a",
        invocation=invocation,
        start_owner_token=START_OWNER_TOKEN,
    )
    with pytest.raises(BudgetConflictError, match="another reservation"):
        await db.repository.bind_invocation(
            db.claimed,
            execution_key="child/b",
            invocation=invocation,
            start_owner_token=START_OWNER_TOKEN,
        )
    assert db.reservations[-1].invocation_id is None


@pytest.mark.parametrize("status", ["RESERVED", "START_INTENT", "SETTLED", "RELEASED"])
async def test_unbound_legacy_rows_never_get_start_permission_or_backfilled_start_history(
    status: str,
) -> None:
    """旧 START_INTENT を読めても実際の options は不明のままにする。"""

    db = BudgetDatabase()
    await db.reserve()
    row = db.reservations[0]
    row.status = status
    if status in {"START_INTENT", "SETTLED"}:
        row.start_intent_at = datetime.now(UTC)
    invocation = budget_invocation(db.claimed)
    with pytest.raises(BudgetUnavailableError, match="binding"):
        await db.repository.start_execution(
            db.claimed,
            execution_key="primary",
            expected_invocation_id=invocation.invocation_id,
            expected_invocation_checksum=invocation.checksum,
            start_owner_token=START_OWNER_TOKEN,
        )
    if status != "RESERVED":
        with pytest.raises(BudgetUnavailableError):
            await db.repository.bind_invocation(
                db.claimed,
                execution_key="primary",
                invocation=invocation,
                start_owner_token=START_OWNER_TOKEN,
            )
    assert row.invocation_id is None and row.invocation_json is None and row.status == status


@pytest.mark.parametrize(
    "damage",
    [
        "id",
        "json",
        "checksum",
        "hash",
        "unknown_version",
        "extra",
        "turns",
        "scope",
        "noncanonical",
    ],
)
async def test_start_and_binding_confirmation_reject_partial_or_corrupt_binding(
    damage: str,
) -> None:
    """hash が存在するだけでは未知版・別実行・部分保存を正本にしない。"""

    db = BudgetDatabase()
    await db.reserve()
    binding = await db.bind()
    row = db.reservations[0]
    assert row.invocation_json is not None
    invocation = AgentInvocation.from_json(row.invocation_json)
    if damage in {"id", "json", "checksum"}:
        setattr(row, f"invocation_{damage}", None)
    elif damage == "hash":
        row.invocation_checksum = "0" * 64
    elif damage == "unknown_version":
        row.invocation_json = {**row.invocation_json, "version": "unknown/v2"}
    elif damage == "extra":
        row.invocation_json = {**row.invocation_json, "prompt": "must-not-be-stored"}
    elif damage == "scope":
        row.run_attempt_id = uuid4()
    elif damage == "noncanonical":
        row.invocation_json = {**row.invocation_json, "run_id": str(invocation.run_id).upper()}
    else:
        row.granted_turns = Decimal(1)
    with pytest.raises((BudgetUnavailableError, LeaseValidationError)):
        await db.repository.start_execution(
            db.claimed, execution_key="primary", **start_arguments(binding)
        )
    with pytest.raises((BudgetUnavailableError, LeaseValidationError)):
        await db.repository.bind_invocation(
            db.claimed,
            execution_key="primary",
            invocation=invocation,
            start_owner_token=START_OWNER_TOKEN,
        )
    assert row.status == "RESERVED"


@pytest.mark.parametrize("already_started", [False, True])
@pytest.mark.parametrize("mismatch", ["id", "checksum"])
async def test_start_checks_explicit_binding_before_first_start_and_before_replay(
    already_started: bool, mismatch: str
) -> None:
    """既存 START_INTENT の False も別 descriptor の正当化には使わせない。"""

    db = BudgetDatabase()
    await db.reserve()
    binding = await db.bind()
    if already_started:
        assert await db.repository.start_execution(
            db.claimed, execution_key="primary", **start_arguments(binding)
        )
    values = start_arguments(binding)
    values["expected_invocation_id" if mismatch == "id" else "expected_invocation_checksum"] = (
        uuid4() if mismatch == "id" else "0" * 64
    )
    with pytest.raises(BudgetConflictError):
        await db.repository.start_execution(db.claimed, execution_key="primary", **values)
    assert db.reservations[0].status == ("START_INTENT" if already_started else "RESERVED")


async def test_binding_confirmation_after_start_does_not_produce_a_second_permission() -> None:
    """原値の確認は起動後にも可能だが、起動の再送は常に False を返す。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    binding = await db.bind()
    assert not await db.repository.start_execution(
        db.claimed, execution_key="primary", **start_arguments(binding)
    )


@pytest.mark.parametrize("invalid", ["expiry", "cancel", "segment", "lease"])
async def test_binding_waits_for_all_locks_and_checks_original_lease(invalid: str) -> None:
    """予算 lock 待機中の失効・取消や予約所有権の差替えは保存前に拒否する。"""

    db = BudgetDatabase()
    await db.reserve()
    if invalid == "expiry":
        db.expire_during_budget_lock = True
    elif invalid == "cancel":
        db.session.scalar.return_value = uuid4()
    else:
        setattr(
            db.reservations[0],
            "run_segment_id" if invalid == "segment" else "execution_lease_hash",
            uuid4() if invalid == "segment" else "0" * 64,
        )
    with pytest.raises(
        RunCancellationRequestedError if invalid == "cancel" else LeaseValidationError
    ):
        await db.bind()
    assert db.reservations[0].invocation_json is None


async def test_binding_and_start_request_fresh_identity_map_rows_in_existing_lock_order() -> None:
    """SQL の再読込指定と lock 順だけを検証し、実際の並行性の証明にはしない。"""

    db = BudgetDatabase()
    await db.reserve()
    for operation in (db.bind, db.start):
        db.session.scalars.reset_mock()
        await operation()
        statements = [call.args[0] for call in db.session.scalars.await_args_list]
        assert [statement.get_final_froms()[0].name for statement in statements[:5]] == [
            "runs",
            "run_segments",
            "run_attempts",
            "run_budget_accounts",
            "run_budget_reservations",
        ]
        assert all(
            statement.get_execution_options().get("populate_existing") is True
            for statement in statements
        )


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_binding_commit_confirms_only_original_metadata(committed: bool) -> None:
    """応答不明時に ID を作り直したり、未保存の binding を確認側で挿入しない。"""

    db = BudgetDatabase()
    await db.reserve()
    invocation = budget_invocation(db.claimed)
    factory = BudgetSessions(db, fail_on=1, committed=committed)
    store = PostgresRunBudgetStore(factory)  # type: ignore[arg-type]
    if committed:
        binding = await store.bind_invocation(
            db.claimed,
            execution_key="primary",
            invocation=invocation,
            start_owner_token=START_OWNER_TOKEN,
        )
        assert binding.invocation_id == invocation.invocation_id
    else:
        with pytest.raises(BudgetUnavailableError, match="not committed"):
            await store.bind_invocation(
                db.claimed,
                execution_key="primary",
                invocation=invocation,
                start_owner_token=START_OWNER_TOKEN,
            )
        assert db.reservations[0].invocation_id is None
    assert len(factory.sessions) == 2
    factory.sessions[1].add.assert_not_called()
    factory.sessions[1].flush.assert_not_awaited()
    assert db.reservations[0].status == "RESERVED"


class ExpiringBudgetSessions(BudgetSessions):
    """flush 中の待機を模し、終了時刻の拒否が transaction 全体を戻すか確認する。"""

    def __call__(self) -> MagicMock:
        """DB の変更後に期限が過ぎても commit を成功させない。"""

        session = super().__call__()

        async def expire() -> None:
            self.database.attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

        session.flush = AsyncMock(side_effect=expire)
        return session


@pytest.mark.parametrize("operation", ["bind", "start"])
async def test_post_flush_execution_expiry_rolls_back_metadata_or_start_intent(
    operation: str,
) -> None:
    """flush が終わるまでの失効を捕捉し、失敗した B の True を返さない。"""

    db = BudgetDatabase()
    await db.reserve()
    invocation = budget_invocation(db.claimed)
    if operation == "start":
        await db.repository.bind_invocation(
            db.claimed,
            execution_key="primary",
            invocation=invocation,
            start_owner_token=START_OWNER_TOKEN,
        )
    store = PostgresRunBudgetStore(ExpiringBudgetSessions(db))  # type: ignore[arg-type]
    with pytest.raises(LeaseValidationError):
        if operation == "bind":
            await store.bind_invocation(
                db.claimed,
                execution_key="primary",
                invocation=invocation,
                start_owner_token=START_OWNER_TOKEN,
            )
        else:
            await store.start_execution(db.claimed, execution_key="primary", **db.bound_arguments())
    assert db.reservations[0].status == "RESERVED" and db.reservations[0].start_intent_at is None
    if operation == "bind":
        assert db.reservations[0].invocation_json is None


@pytest.mark.parametrize("constraint", ["uq_run_budget_invocation_id", "another_constraint", None])
async def test_binding_maps_only_the_global_invocation_unique_violation(
    constraint: str | None,
) -> None:
    """FK・未知の DB 失敗を別 invocation の既知競合として隠さない。"""

    db = BudgetDatabase()
    await db.reserve()
    original = Exception("private database diagnostic")
    if constraint is not None:
        original.constraint_name = constraint  # type: ignore[attr-defined]
    error = IntegrityError("private SQL", {}, original)
    db.session.flush.side_effect = error
    with pytest.raises(
        BudgetConflictError if constraint == "uq_run_budget_invocation_id" else IntegrityError
    ) as caught:
        await db.bind()
    if constraint == "uq_run_budget_invocation_id":
        assert "private" not in str(caught.value)
    else:
        assert caught.value is error


@pytest.mark.parametrize("value", ["ABC", "0" * 63, "A" * 64, None])
def test_binding_dto_rejects_invalid_checksum(value: object) -> None:
    """照合 DTO が非 canonical な入力を便利変換しない。"""

    with pytest.raises(BudgetError):
        BudgetInvocationBinding(uuid4(), uuid4(), value)  # type: ignore[arg-type]


@pytest.mark.parametrize("operation", ["bind", "start"])
async def test_cancellation_query_wait_cannot_outlive_the_execution_lease(operation: str) -> None:
    """取消がない場合も最後の await 後に時刻を確認し、期限切れを成功へ流さない。"""

    db = BudgetDatabase()
    await db.reserve()
    if operation == "start":
        await db.bind()

    async def expire(_: object) -> None:
        db.attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    db.session.scalar.side_effect = expire
    with pytest.raises(LeaseValidationError):
        if operation == "bind":
            await db.bind()
        else:
            await db.repository.start_execution(
                db.claimed, execution_key="primary", **db.bound_arguments()
            )
    assert db.reservations[0].status == "RESERVED"
    if operation == "bind":
        assert db.reservations[0].invocation_id is None


@pytest.mark.parametrize("failure", ["expired", "cancelled", "blocked"])
async def test_binding_readback_still_checks_current_authority(failure: str) -> None:
    """保存済み metadata の確認を、現在の lease/取消の検証なしで返さない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.bind()
    if failure == "expired":
        db.attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif failure == "cancelled":
        db.session.scalar.return_value = uuid4()
    else:
        db.account.block_code = "usage_unverifiable"
    if failure == "blocked":
        binding = await db.bind()
        with pytest.raises(BudgetUnavailableError):
            await db.repository.start_execution(
                db.claimed, execution_key="primary", **start_arguments(binding)
            )
    else:
        with pytest.raises(
            LeaseValidationError if failure == "expired" else RunCancellationRequestedError
        ):
            await db.bind()
    assert db.reservations[0].status == "RESERVED"
