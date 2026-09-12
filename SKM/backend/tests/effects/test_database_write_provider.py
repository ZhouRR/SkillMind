"""PG Provider が原 Effect の回执・権限・不明結果を Worker port に保持することを検証する。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from skillmind.effects.database_provider import DatabaseWriteProvider
from skillmind.effects.database_write import database_row_revision
from skillmind.effects.postgres_write import (
    DatabaseWriteConflictError,
    DatabaseWriteReceipt,
    DatabaseWriteUncertainError,
    PostgresDatabaseWriteSource,
)
from skillmind.effects.redmine import EffectProviderStaleError, EffectProviderTransportError
from tests.effects.database_fixtures import database_execution


def source():
    """TCP/SQL を使わず原回执だけを返す Provider port double。"""

    result = AsyncMock(spec=PostgresDatabaseWriteSource)
    result.lookup.return_value = None
    result.apply.return_value = DatabaseWriteReceipt(
        before=None, after={"id": "row-1", "status": "RUNNING"}, replayed=False
    )
    return result


async def test_insert_emits_original_transaction_evidence_and_calls_authorizer() -> None:
    """単行変更と Evidence が同じ Effect/checksum に束縛される。"""

    db, authorize, execution = source(), AsyncMock(), database_execution()
    provider = DatabaseWriteProvider(source=db, authorize=authorize)
    result = await provider.apply(execution, credential="unit-test-value")
    authorize.assert_awaited_once_with(execution, "unit-test-value")
    db.lookup.assert_not_awaited()
    command = db.apply.call_args.args[2]
    assert command.effect_id == execution.effect_execution_id
    assert result.before.content == {"row": None}
    assert result.after.content == {"row": {"id": "row-1", "status": "RUNNING"}}
    assert result.verification["request_checksum"] == command.checksum
    assert result.after.source_locator["effect_id"] == str(execution.effect_execution_id)
    assert result.verification["before_revision"] == "absent"
    await db.apply.call_args.kwargs["authorize"]()
    assert authorize.await_count == 2


async def test_retry_returns_saved_receipt_without_another_write() -> None:
    """再 claim は現在行でなく元回执を返し、apply を呼ばない。"""

    db = source()
    db.lookup.return_value = DatabaseWriteReceipt(None, {"id": "row-1", "status": "OLD"}, True)
    execution = replace(database_execution(), attempt_no=2)
    result = await DatabaseWriteProvider(source=db, authorize=AsyncMock()).apply(
        execution, credential="unit-test-value"
    )
    db.apply.assert_not_awaited()
    assert result.replayed
    assert result.after.content["row"]["status"] == "OLD"
    assert db.lookup.call_args.args[2].effect_id == execution.effect_execution_id


async def test_absent_lookup_uses_same_command_for_serialized_transaction() -> None:
    """不在観測後も同じ命令の lock/回执再検証に委ね、別 identity を生成しない。"""

    db = source()
    execution = replace(database_execution(), attempt_no=2)
    await DatabaseWriteProvider(source=db, authorize=AsyncMock()).apply(
        execution, credential="unit-test-value"
    )
    assert db.lookup.call_args.args[2] is db.apply.call_args.args[2]
    assert db.lookup.call_args.kwargs["authorize"] is db.apply.call_args.kwargs["authorize"]


@pytest.mark.parametrize("phase", ["lookup", "apply"])
async def test_uncertain_response_preserves_unknown_without_in_call_retry(phase) -> None:
    """SQL/接続エラーの本文を公開せず、元 Effect を要核対の retry として返す。"""

    db = source()
    getattr(db, phase).side_effect = DatabaseWriteUncertainError("private detail")
    execution = replace(database_execution(), attempt_no=2)
    with pytest.raises(EffectProviderTransportError) as raised:
        await DatabaseWriteProvider(source=db, authorize=AsyncMock()).apply(
            execution, credential="unit-test-value"
        )
    assert raised.value.code == "database_effect_uncertain"
    assert raised.value.retryable
    assert "private" not in str(raised.value)
    assert getattr(db, phase).await_count == 1
    if phase == "lookup":
        db.apply.assert_not_awaited()


async def test_revocation_prevents_connection_and_is_not_retryable() -> None:
    """批准/凭据の再検証が失敗したら SQL port に到達しない。"""

    db, authorize = source(), AsyncMock(side_effect=PermissionError("revoked"))
    with pytest.raises(EffectProviderTransportError) as raised:
        await DatabaseWriteProvider(source=db, authorize=authorize).apply(
            database_execution(), credential="unit-test-value"
        )
    assert raised.value.code == "effect_authority_revoked"
    assert not raised.value.retryable
    db.apply.assert_not_awaited()
    db.lookup.assert_not_awaited()


async def test_conflict_and_cancellation_are_not_converted_to_replay() -> None:
    """古い原行は STALE、task 取消は呼出元へ伝播し、成功を作らない。"""

    db = source()
    provider = DatabaseWriteProvider(source=db, authorize=AsyncMock())
    db.apply.side_effect = DatabaseWriteConflictError("changed")
    with pytest.raises(EffectProviderStaleError):
        await provider.apply(database_execution(), credential="unit-test-value")
    db.apply.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await provider.apply(database_execution(), credential="unit-test-value")


async def test_external_mutation_after_authorization_cannot_retarget_execution() -> None:
    """最初の await 中の元 JSON 変更は SQL/再検証の固定 snapshot に届かない。"""

    db, execution = source(), database_execution()
    original = deepcopy(execution)

    async def authorize(frozen, credential):
        """caller が共有していた JSON を待機中に変更する競争を再現する。"""
        assert frozen == original
        execution.integration_config["host"] = "other.example.test"
        execution.changes[0]["value"]["values"]["status"] = "CHANGED"

    await DatabaseWriteProvider(source=db, authorize=authorize).apply(
        execution, credential="unit-test-value"
    )
    assert db.apply.call_args.args[0] == original.integration_config
    assert db.apply.call_args.args[2].values == {"status": "RUNNING"}


@pytest.mark.parametrize(
    "change",
    [
        {"operation": "DELETE"},
        {"capability_version": "repository.write/v1"},
        {"precondition": {"revision": "guessed"}},
        {"attempt_no": 0},
        {"changes": ({"path": "/row", "action": "SET", "value": {"sql": "UPDATE x"}},)},
        {"verification": {"method": "READ_BACK", "paths": ["/different"]}},
    ],
)
async def test_invalid_proposal_never_reaches_authorization_or_sql(change) -> None:
    """汎用 proposal shape でも DB 単行契約外の指示を接続前に拒否する。"""

    db, authorize = source(), AsyncMock()
    with pytest.raises(ValueError):
        await DatabaseWriteProvider(source=db, authorize=authorize).apply(
            replace(database_execution(), **change), credential="unit-test-value"
        )
    authorize.assert_not_awaited()
    db.apply.assert_not_awaited()


async def test_update_requires_full_original_primary_key_and_revision() -> None:
    """更新の原行/主キー/hash を別々に作り替える提案を拒否する。"""

    db, execution = source(), database_execution()
    before = {"id": "row-1", "status": "OLD"}
    execution.changes[0]["value"]["expected"] = before
    execution = replace(
        execution,
        operation="UPDATE",
        precondition={
            "revision": database_row_revision(before),
        },
    )
    await DatabaseWriteProvider(source=db, authorize=AsyncMock()).apply(
        execution, credential="unit-test-value"
    )
    assert db.apply.call_args.args[2].expected == before
    before["id"] = "other"
    execution = replace(execution, precondition={"revision": database_row_revision(before)})
    with pytest.raises(ValueError, match="primary key"):
        await DatabaseWriteProvider(source=db, authorize=AsyncMock()).apply(
            execution, credential="unit-test-value"
        )


@pytest.mark.parametrize("uncertain", [False, True])
async def test_worker_preserves_database_facts_and_unknown_classification(uncertain) -> None:
    """実 ApprovedEffectExecutor を通し、Provider の事実と unknown を永続化 port へ渡す。"""

    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from skillmind.effects.database_write import (
        DATABASE_WRITE_CAPABILITY,
        DATABASE_WRITE_PROVIDER_VERSION,
    )
    from skillmind.effects.domain import EffectExecutionStatus
    from skillmind.effects.provider import EffectProviderDefinition, EffectProviderRegistry
    from skillmind.worker.effects import ApprovedEffectExecutor

    db, execution = source(), database_execution()
    if uncertain:
        db.apply.side_effect = DatabaseWriteUncertainError("lost response")
    service = MagicMock()
    service.claim_effect_execution = AsyncMock(return_value=execution)
    service.resolve_secret_reference = AsyncMock(return_value=object())
    service.finalize_effect_execution = AsyncMock(
        return_value=SimpleNamespace(
            status=EffectExecutionStatus.REQUESTED if uncertain else EffectExecutionStatus.APPLIED,
        )
    )
    resolver = MagicMock()
    resolver.resolve.return_value = "unit-test-value"
    registry = EffectProviderRegistry(
        (
            EffectProviderDefinition(
                capability_version=DATABASE_WRITE_CAPABILITY,
                provider="postgres",
                provider_version=DATABASE_WRITE_PROVIDER_VERSION,
                implementation=DatabaseWriteProvider(source=db, authorize=AsyncMock()),
                requires_secret=True,
            ),
        )
    )
    executor = ApprovedEffectExecutor(
        effect_service=service,
        provider_registry=registry,
        secret_resolver=resolver,
        worker_id="test-worker",
        lease_seconds=30,
        max_attempts=3,
    )
    await executor.execute(execution.effect_execution_id)
    final = service.finalize_effect_execution.call_args
    assert final.args[0] is execution
    if uncertain:
        assert final.kwargs["result"] is None
        assert final.kwargs["failure"].code == "database_effect_uncertain"
        assert final.kwargs["failure"].retryable
    else:
        assert final.kwargs["failure"] is None
        assert final.kwargs["result"].after.source_locator["effect_id"] == str(
            execution.effect_execution_id
        )
