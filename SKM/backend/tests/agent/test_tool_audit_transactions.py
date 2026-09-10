"""実 Tool audit writer の取引・実行権を SQL/commit fake で検証する。実 DB 証明ではない。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList
from sqlalchemy.sql.operators import and_, eq

from skillmind.agent.evidence import (
    EvidenceDraft,
    EvidenceRecord,
    PostgresToolAuditWriter,
    ToolAuditLease,
    ToolInvocation,
    invocation_fingerprint,
)
from skillmind.core.hashing import sha256_hex
from skillmind.db.models import Evidence, PermissionDecision, RunAttempt, RunSegment, ToolCall
from skillmind.runs.domain import (
    LeaseValidationError,
    RunCancellationRequestedError,
    SessionContinuationMode,
)
from tests.agent.test_tool_gateway import CsvIssueProvider, _context, _registry
from tests.runs.test_execution_gates import execution_rows


def _matches(row: Any, expression: Any) -> bool:
    """試験対象の equality WHERE を評価し、未知 SQL を無条件の成功へ変換しない。"""

    if isinstance(expression, BooleanClauseList) and expression.operator is and_:
        return all(_matches(row, item) for item in expression.clauses)
    if isinstance(expression, BinaryExpression) and expression.operator is eq:
        assert isinstance(expression.right, BindParameter)
        key = expression.left.key
        assert isinstance(key, str)
        return bool(getattr(row, key) == expression.right.value)
    raise AssertionError(f"Unsupported audit test predicate: {expression}")


class AuditTransaction:
    """行の rollback、確定後の応答喪失、commit 待機を明示的に区別する。"""

    def __init__(self, database: AuditDatabase) -> None:
        """新規 audit 行と既存行の field 値を取引開始時に固定する。"""

        self.database = database
        self.saved_rows = list(database.audit_rows)
        self.saved_values = [
            (row, {
                column.key: deepcopy(getattr(row, column.key))
                for column in row.__table__.columns
            })
            for row in database.audit_rows
        ]

    async def __aenter__(self) -> Self:
        """Service が開始する transaction に入る。"""

        self.database.timeline.append("begin")
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: object,
    ) -> None:
        """外部取消や不明 commit を返却成功へ置き換えず、確定した事実だけを残す。"""

        del error, traceback
        database = self.database
        if kind is None and database.commit_entered is not None:
            database.commit_entered.set()
            assert database.commit_release is not None
            await database.commit_release.wait()
        failure = database.commit_failure
        database.commit_failure = None
        if kind is not None or failure == "before":
            database.audit_rows[:] = self.saved_rows
            for row, values in self.saved_values:
                for key, value in values.items():
                    setattr(row, key, value)
            database.timeline.append("rollback")
        else:
            database.timeline.append("commit")
        if kind is None and failure is not None:
            raise ConnectionError("synthetic commit response unavailable")


class AuditDatabase:
    """本物の ORM 行を SQL 条件で選び、I/O の各待機境界を観測できる局部 fake。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """原 expiry を固定し、テストでは now だけを進めて期限通過を作る。"""

        self.claimed, self.run, self.segment, self.attempt = execution_rows()
        self.now = datetime(2026, 9, 10, 12, tzinfo=UTC)
        self.expiry = self.now + timedelta(seconds=60)
        self.claimed = replace(self.claimed, lease_expires_at=self.expiry)
        self.attempt.lease_expires_at = self.expiry
        self.run.permission_snapshot_json = {"actor_id": str(self.claimed.actor_id)}
        self.run.input_json = {}
        self.run.selected_sources_json = {}
        self.run.limits_snapshot_json = {}
        self.segment.continuation_mode = SessionContinuationMode.INITIAL.value
        self.attempts = [self.attempt]
        self.segments = [self.segment]
        self.audit_rows: list[Any] = []
        self.timeline: list[str] = []
        self.queries: list[Any] = []
        self.sessions: list[MagicMock] = []
        self.cancelled = False
        self.scope_active = True
        self.after_tool_lock: str | None = None
        self.on_flush: str | None = None
        self.commit_failure: str | None = None
        self.commit_entered: asyncio.Event | None = None
        self.commit_release: asyncio.Event | None = None
        clock = MagicMock(wraps=datetime)
        clock.now.side_effect = lambda _timezone: self.now
        monkeypatch.setattr("skillmind.agent.evidence.datetime", clock)
        monkeypatch.setattr("skillmind.runs.repository.datetime", clock)
        self.writer = PostgresToolAuditWriter(
            self, claimed_run=self.claimed, authority_check=self.require_active,  # type: ignore[arg-type]
        )
        registry = _registry(CsvIssueProvider())
        tool = registry.resolve("issue.read/v1", provider="csv", integration_id=uuid4())
        arguments = {"issue_ref": "FIXTURE-1", "purpose": "analysis"}
        self.invocation = ToolInvocation(
            run_id=self.claimed.run_id, run_attempt_id=self.claimed.run_attempt_id,
            agent_session_id=uuid4(), sdk_tool_use_id="synthetic-original-tool-use", tool=tool,
            arguments=arguments,
            request_fingerprint=invocation_fingerprint(tool.sdk_name, arguments),
        )

    def __call__(self) -> MagicMock:
        """各 writer call に独立 session を渡し、ORM identity map の再利用は仮定しない。"""

        session = MagicMock(spec=AsyncSession)
        session.__aenter__.return_value = session
        session.begin.side_effect = lambda: AuditTransaction(self)
        session.scalars = AsyncMock(side_effect=self.scalars)
        session.scalar = AsyncMock(side_effect=self.scalar)
        session.get = AsyncMock(side_effect=self.get)
        session.flush = AsyncMock(side_effect=self.flush)
        session.add.side_effect = self.audit_rows.append
        session.add_all.side_effect = self.audit_rows.extend
        self.sessions.append(session)
        return session

    def require_active(self) -> None:
        """試験が所有する実行範囲を閉じた後、古い callback に新しい書込権を戻さない。"""

        if not self.scope_active:
            raise LeaseValidationError("Synthetic execution owner is closed")

    def _rows(self, table: str) -> list[Any]:
        """対象外 table を受理せず、Run aggregate と audit 行の実集合だけを返す。"""

        execution_rows_by_table: dict[str, list[Any]] = {
            "runs": [self.run], "run_segments": self.segments, "run_attempts": self.attempts,
        }
        if table in execution_rows_by_table:
            return execution_rows_by_table[table]
        assert table in {"tool_calls", "evidence", "permission_decisions"}
        return [row for row in self.audit_rows if row.__table__.name == table]

    async def scalars(self, statement: Any) -> MagicMock:
        """FOR UPDATE と実際の絞込みを保持し、取得完了直後の期限/取消を注入する。"""

        self.queries.append(statement)
        table = statement.get_final_froms()[0].name
        self.timeline.append(table)
        rows = [row for row in self._rows(table)
                if all(_matches(row, item) for item in statement._where_criteria)]
        result = MagicMock()
        assert len(rows) <= 1
        result.one_or_none.return_value = rows[0] if rows else None
        result.all.return_value = rows
        if table == "tool_calls":
            self._inject(self.after_tool_lock)
            self.after_tool_lock = None
        return result

    async def scalar(self, statement: Any) -> UUID | None:
        """取消 query を実 Run identity と照合し、他の scalar は暗黙受理しない。"""

        self.queries.append(statement)
        if statement.get_final_froms()[0].name == "run_attempts":
            rows = [row for row in self.attempts
                    if all(_matches(row, item) for item in statement._where_criteria)]
            assert len(rows) <= 1
            self.timeline.append("origin_attempt")
            return rows[0].id if rows else None
        values = statement.compile().params.values()
        assert "RUN_CANCEL_REQUESTED" in values and self.run.id in values
        self.timeline.append("cancel")
        return uuid4() if self.cancelled else None

    async def get(self, model: Any, identity: UUID, **options: Any) -> Any:
        """ToolCall の直接 PK lookup でも lock の存在と exact identity を確認する。"""

        assert model is ToolCall and options.get("with_for_update")
        self.timeline.append("tool_calls")
        self._inject(self.after_tool_lock)
        self.after_tool_lock = None
        return next((row for row in self._rows("tool_calls") if row.id == identity), None)

    async def flush(self) -> None:
        """INSERT/UPDATE 待機中の期限/取消/失敗が最終 gate を通過しないことを観測する。"""

        self.timeline.append("flush")
        action, self.on_flush = self.on_flush, None
        self._inject(action)
        if action == "error":
            raise ConnectionError("synthetic flush unavailable")
        if action == "task_cancel":
            raise asyncio.CancelledError

    def _inject(self, action: str | None) -> None:
        """既定 expiry を変更せず時刻を進め、取消は独立した持久意図として保持する。"""

        if action == "expiry":
            self.now = self.expiry
        elif action == "cancel":
            self.cancelled = True
        elif action == "scope_close":
            self.scope_active = False

    def rows(self, model: Any) -> list[Any]:
        """commit/rollback 後の正本に該当 model が何件あるか検査する。"""

        return [row for row in self.audit_rows if isinstance(row, model)]

    async def start(self) -> ToolAuditLease:
        """本来の writer を使い、取引開始から確定まで実行する。"""

        return await self.writer.start_authorized(self.invocation)

    async def complete(self, lease: ToolAuditLease) -> dict[str, Any]:
        """Evidence と成功 Tool response を同じ writer transaction へ渡す。"""

        ref = "ev_synthetic_original"
        return await self.writer.complete(
            lease, result={
                "status": "success", "provider": "csv", "evidence_refs": [ref],
                "issue": {
                    "id": "FIXTURE-1", "subject": "Synthetic original issue",
                    "description": None, "status": "open", "updated_at": None,
                    "fields": {}, "extensions": {},
                },
                "warnings": [], "truncated": False,
            },
            evidence=(EvidenceRecord(ref, EvidenceDraft(
                evidence_type="synthetic-fixture", source_uri="fixture://audit/original",
                source_locator={"item": "original"},
                content_hash=f"sha256:{sha256_hex(b'original')}",
            )),), duration_ms=10,
        )

    async def fail(self, lease: ToolAuditLease) -> None:
        """失敗を新しい成功や再実行許可へ変換せず、分類だけを保存する。"""

        await self.writer.fail(lease, code="unavailable", retryable=False, duration_ms=10)

    def continue_run(self, mode: SessionContinuationMode, *, same_sdk: bool = True) -> None:
        """新しい Segment/Attempt の有効 claim を作り、旧 Tool 行の Attempt を保持する。"""

        original_attempt = self.attempt
        original_segment = self.segment
        self.segment = RunSegment(
            id=uuid4(), run_id=self.run.id, segment_no=original_segment.segment_no + 1,
            status="RUNNING",
            continuation_mode=mode.value,
        )
        self.attempt = RunAttempt(
            id=uuid4(), run_id=self.run.id, run_segment_id=self.segment.id, status="RUNNING",
            lease_token_hash=original_attempt.lease_token_hash, lease_expires_at=self.expiry,
        )
        original_attempt.status = "SUCCEEDED"
        original_attempt.lease_token_hash = None
        original_segment.status = "COMPLETED"
        self.attempts.append(self.attempt)
        self.segments.append(self.segment)
        self.claimed = replace(
            self.claimed, run_attempt_id=self.attempt.id, run_segment_id=self.segment.id,
            segment_no=self.segment.segment_no, parent_run_attempt_id=original_attempt.id,
            parent_sdk_session_id=self.invocation.agent_session_id if same_sdk else uuid4(),
            continuation_mode=mode,
        )
        self.writer = PostgresToolAuditWriter(
            self, claimed_run=self.claimed, authority_check=self.require_active,  # type: ignore[arg-type]
        )
        self.invocation = replace(self.invocation, run_attempt_id=self.attempt.id)


async def test_start_and_completion_use_full_lock_order_and_commit_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run→Segment→Attempt→ToolCall の取得後に期限を検証し、原 identity を返す。"""

    database = AuditDatabase(monkeypatch)
    lease = await database.start()
    assert lease.invocation == database.invocation and lease.is_new is True
    assert database.timeline.index("run_attempts") < database.timeline.index("tool_calls")
    assert database.timeline[:4] == ["begin", "runs", "run_segments", "run_attempts"]
    assert database.timeline[-1] == "commit"
    assert len(database.rows(ToolCall)) == len(database.rows(PermissionDecision)) == 1
    database.timeline.clear()
    await database.writer.verify_dispatch(lease)
    assert database.timeline[:4] == ["begin", "runs", "run_segments", "run_attempts"]
    result = await database.complete(lease)
    assert result["status"] == "success"
    assert result["evidence_refs"] == ["ev_synthetic_original"]
    assert database.rows(ToolCall)[0].result_json == result
    assert database.rows(ToolCall)[0].status == "SUCCEEDED"
    assert len(database.rows(Evidence)) == 1
    for statement in database.queries:
        if statement.get_final_froms()[0].name in {"runs", "run_segments", "run_attempts"}:
            assert "FOR UPDATE" in str(statement)
            assert statement.get_execution_options().get("populate_existing") is True


@pytest.mark.parametrize("action", ["start", "verify_dispatch", "complete", "fail"])
@pytest.mark.parametrize("invalid", ["token", "expiry", "run", "segment", "attempt", "cancel"])
async def test_every_audit_operation_rejects_expired_cancelled_or_terminal_execution(
    monkeypatch: pytest.MonkeyPatch, action: str, invalid: str,
) -> None:
    """Provider の前後とも現在の fencing を要求し、拒否後に監査を追加/変更しない。"""

    database = AuditDatabase(monkeypatch)
    lease = await database.start() if action != "start" else None
    before = [(row, {
        column.key: deepcopy(getattr(row, column.key)) for column in row.__table__.columns
    }) for row in database.audit_rows]
    if invalid == "token":
        database.attempt.lease_token_hash = "0" * 64
    elif invalid == "expiry":
        database.now = database.expiry
    elif invalid == "run":
        database.run.status = "SUCCEEDED"
    elif invalid == "segment":
        database.segment.status = "WAITING"
    elif invalid == "attempt":
        database.attempt.status = "FAILED"
    else:
        database.cancelled = True
    with pytest.raises((LeaseValidationError, RunCancellationRequestedError)):
        if action == "start":
            await database.start()
        elif action == "verify_dispatch":
            assert lease is not None
            await database.writer.verify_dispatch(lease)
        elif action == "complete":
            assert lease is not None
            await database.complete(lease)
        else:
            assert lease is not None
            await database.fail(lease)
    assert database.audit_rows == [row for row, _ in before]
    for row, values in before:
        assert {key: getattr(row, key) for key in values} == values


@pytest.mark.parametrize("boundary", ["after_tool_lock", "on_flush"])
@pytest.mark.parametrize("invalid", ["expiry", "cancel", "scope_close"])
@pytest.mark.parametrize("action", ["start", "complete", "fail"])
async def test_post_wait_gate_rejects_fixed_expiry_or_cancellation_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, boundary: str, invalid: str, action: str,
) -> None:
    """lock/flush 前の now を流用せず、元の有効期限を跨いだ新しい書込を取消す。"""

    database = AuditDatabase(monkeypatch)
    lease = await database.start() if action != "start" else None
    setattr(database, boundary, invalid)
    with pytest.raises((LeaseValidationError, RunCancellationRequestedError)):
        if action == "start":
            await database.start()
        elif action == "complete":
            assert lease is not None
            await database.complete(lease)
        else:
            assert lease is not None
            await database.fail(lease)
    assert database.attempt.lease_expires_at == database.expiry
    assert not database.rows(Evidence)
    assert all(row.status == "RUNNING" for row in database.rows(ToolCall))
    if action == "start":
        assert not database.audit_rows


@pytest.mark.parametrize("action", ["verify_dispatch", "complete", "fail"])
async def test_bare_lease_never_authorizes_dispatch_or_audit_write(
    monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    """実在する ToolCall UUID だけを知る呼出元に、原実行の変更権を付与しない。"""

    database = AuditDatabase(monkeypatch)
    original = await database.start()
    bare = ToolAuditLease(original.tool_call_id, "RUNNING")
    with pytest.raises((ValueError, RuntimeError)):
        if action == "verify_dispatch":
            await database.writer.verify_dispatch(bare)
        elif action == "complete":
            await database.complete(bare)
        else:
            await database.fail(bare)
    assert database.rows(ToolCall)[0].status == "RUNNING"
    assert not database.rows(Evidence)


@pytest.mark.parametrize("field", [
    "run_id", "run_attempt_id", "agent_session_id", "sdk_tool_use_id", "request_fingerprint",
    "arguments", "arguments_rehashed", "capability", "sdk_name", "provider", "integration_id",
])
@pytest.mark.parametrize("action", ["verify_dispatch", "complete", "fail"])
async def test_original_invocation_identity_cannot_be_swapped_after_admission(
    monkeypatch: pytest.MonkeyPatch, field: str, action: str,
) -> None:
    """Fingerprint 単独で許可せず、scope/SDK/session/Tool binding の原 identity を全照合する。"""

    database = AuditDatabase(monkeypatch)
    lease = await database.start()
    invocation = database.invocation
    if field in {"capability", "sdk_name", "provider", "integration_id"}:
        value: Any = uuid4() if field == "integration_id" else "different-tool-binding"
        invocation = replace(invocation, tool=replace(invocation.tool, **{field: value}))
    else:
        value = uuid4() if field.endswith("_id") and field != "sdk_tool_use_id" else "different"
        if field == "request_fingerprint":
            value = "0" * 64
        elif field in {"arguments", "arguments_rehashed"}:
            value = {"issue_ref": "OTHER", "purpose": "analysis"}
        if field == "arguments_rehashed":
            invocation = replace(
                invocation, arguments=value,
                request_fingerprint=invocation_fingerprint(invocation.tool.sdk_name, value),
            )
        else:
            invocation = replace(invocation, **{field: value})
    forged = replace(lease, invocation=invocation)
    with pytest.raises((ValueError, RuntimeError)):
        if action == "verify_dispatch":
            await database.writer.verify_dispatch(forged)
        elif action == "complete":
            await database.complete(forged)
        else:
            await database.fail(forged)
    assert database.rows(ToolCall)[0].status == "RUNNING"
    assert not database.rows(Evidence)


@pytest.mark.parametrize("terminal", ["RUNNING", "FAILED", "SUCCEEDED"])
async def test_existing_call_is_never_a_new_dispatch_grant(
    monkeypatch: pytest.MonkeyPatch, terminal: str,
) -> None:
    """再読取は成功結果の返却か未決/失敗であり、同じ Provider の再実行許可ではない。"""

    database = AuditDatabase(monkeypatch)
    first = await database.start()
    if terminal == "FAILED":
        await database.fail(first)
    elif terminal == "SUCCEEDED":
        await database.complete(first)
    replay = await database.start()
    assert replay.is_new is False and replay.status == terminal
    if terminal == "SUCCEEDED":
        await database.writer.verify_dispatch(replay)
    else:
        with pytest.raises((ValueError, RuntimeError)):
            await database.writer.verify_dispatch(replay)
    assert len(database.rows(ToolCall)) == 1
    assert len(database.rows(PermissionDecision)) == 1
    assert (replay.result is not None) is (terminal == "SUCCEEDED")


async def test_resume_replays_original_success_without_reparenting_the_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じ SDK transcript の再開は旧成功を読めるが、新 Attempt の成功へコピーしない。"""

    database = AuditDatabase(monkeypatch)
    first = await database.start()
    result = await database.complete(first)
    original_attempt = database.rows(ToolCall)[0].run_attempt_id
    database.continue_run(SessionContinuationMode.RESUME)
    replay = await database.start()
    assert replay.is_new is False and replay.status == "SUCCEEDED" and replay.result == result
    await database.writer.verify_dispatch(replay)
    assert (await database.complete(replay)) == result
    await database.fail(replay)
    assert len(database.rows(ToolCall)) == len(database.rows(Evidence)) == 1
    assert database.rows(ToolCall)[0].run_attempt_id == original_attempt
    assert database.rows(ToolCall)[0].status == "SUCCEEDED"


@pytest.mark.parametrize("mode,same_sdk,original_status", [
    (SessionContinuationMode.INITIAL, True, "SUCCEEDED"),
    (SessionContinuationMode.FORK, True, "SUCCEEDED"),
    (SessionContinuationMode.REPLACE, True, "SUCCEEDED"),
    (SessionContinuationMode.RESUME, False, "SUCCEEDED"),
    (SessionContinuationMode.RESUME, True, "RUNNING"),
    (SessionContinuationMode.RESUME, True, "FAILED"),
])
async def test_cross_attempt_replay_requires_success_and_original_sdk_resume(
    monkeypatch: pytest.MonkeyPatch, mode: SessionContinuationMode,
    same_sdk: bool, original_status: str,
) -> None:
    """新しい呼出方式や別 SDK session、未決/失敗を旧成功の再利用に見せかけない。"""

    database = AuditDatabase(monkeypatch)
    lease = await database.start()
    if original_status == "SUCCEEDED":
        await database.complete(lease)
    elif original_status == "FAILED":
        await database.fail(lease)
    original_attempt = database.rows(ToolCall)[0].run_attempt_id
    database.continue_run(mode, same_sdk=same_sdk)
    with pytest.raises((ValueError, RuntimeError)):
        await database.start()
    assert len(database.rows(ToolCall)) == 1
    assert database.rows(ToolCall)[0].run_attempt_id == original_attempt
    assert database.rows(ToolCall)[0].status == original_status


@pytest.mark.parametrize("origin", ["missing", "foreign"])
async def test_resume_rejects_original_call_with_missing_or_cross_run_attempt(
    monkeypatch: pytest.MonkeyPatch, origin: str,
) -> None:
    """独立 FK が許す Run/Attempt の矛盾を、同じ SDK session の表示だけで信用しない。"""

    database = AuditDatabase(monkeypatch)
    await database.complete(await database.start())
    call = database.rows(ToolCall)[0]
    call.run_attempt_id = uuid4()
    if origin == "foreign":
        database.attempts.append(RunAttempt(id=call.run_attempt_id, run_id=uuid4()))
    database.continue_run(SessionContinuationMode.RESUME)
    with pytest.raises((ValueError, RuntimeError)):
        await database.start()
    assert call.status == "SUCCEEDED" and len(database.rows(Evidence)) == 1


async def test_same_sdk_resume_can_read_success_from_an_earlier_ancestor_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接 parent だけに限定せず、同 Run 内の transcript に保持された原成功を再利用する。"""

    database = AuditDatabase(monkeypatch)
    result = await database.complete(await database.start())
    original_attempt = database.rows(ToolCall)[0].run_attempt_id
    database.continue_run(SessionContinuationMode.RESUME)
    database.continue_run(SessionContinuationMode.RESUME)
    assert database.claimed.parent_run_attempt_id != original_attempt
    replay = await database.start()
    assert replay.is_new is False and replay.result == result
    assert database.rows(ToolCall)[0].run_attempt_id == original_attempt


@pytest.mark.parametrize("action", ["start", "verify_dispatch", "complete", "fail"])
async def test_closed_execution_owner_rejects_even_still_live_database_lease(
    monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    """DB lease が残っていても scope 終了後の孤児 callback を受理しない。"""

    database = AuditDatabase(monkeypatch)
    lease = await database.start() if action != "start" else None
    database.scope_active = False
    with pytest.raises(LeaseValidationError):
        if action == "start":
            await database.start()
        elif action == "verify_dispatch":
            assert lease is not None
            await database.writer.verify_dispatch(lease)
        elif action == "complete":
            assert lease is not None
            await database.complete(lease)
        else:
            assert lease is not None
            await database.fail(lease)
    assert database.attempt.lease_expires_at is not None
    assert database.attempt.lease_expires_at > database.now
    assert not database.rows(Evidence)
    assert all(row.status == "RUNNING" for row in database.rows(ToolCall))


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("action", ["start", "complete", "fail"])
async def test_unknown_commit_never_returns_success_or_dispatch_permission(
    monkeypatch: pytest.MonkeyPatch, committed: bool, action: str,
) -> None:
    """実際に commit された場合も応答喪失は例外のまま残し、確定済み記録を上書きしない。"""

    database = AuditDatabase(monkeypatch)
    lease = await database.start() if action != "start" else None
    database.commit_failure = "after" if committed else "before"
    with pytest.raises(ConnectionError):
        if action == "start":
            await database.start()
        elif action == "complete":
            assert lease is not None
            await database.complete(lease)
        else:
            assert lease is not None
            await database.fail(lease)
    calls = database.rows(ToolCall)
    if action == "start":
        assert bool(calls) is committed
    else:
        assert calls[0].status == (
            {"complete": "SUCCEEDED", "fail": "FAILED"}[action] if committed else "RUNNING"
        )
    assert bool(database.rows(Evidence)) is (committed and action == "complete")
    if calls:
        assert (await database.start()).is_new is False


async def test_start_permission_is_not_available_while_commit_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run/Tool 行の変更が見えても transaction 終了までは Provider 起動権を渡さない。"""

    database = AuditDatabase(monkeypatch)
    database.commit_entered, database.commit_release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(database.start())
    try:
        await asyncio.wait_for(database.commit_entered.wait(), 2)
        assert not task.done()
        database.commit_release.set()
        assert (await task).is_new is True
    finally:
        database.commit_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", ["error", "task_cancel"])
@pytest.mark.parametrize("action", ["start", "complete", "fail"])
async def test_flush_failure_or_native_cancellation_rolls_back_without_success(
    monkeypatch: pytest.MonkeyPatch, failure: str, action: str,
) -> None:
    """flush の接続障害や取消を成功/失敗の偽確定へ変換せず、元の状態へ戻す。"""

    database = AuditDatabase(monkeypatch)
    lease = await database.start() if action != "start" else None
    database.on_flush = failure
    with pytest.raises(ConnectionError if failure == "error" else asyncio.CancelledError):
        if action == "start":
            await database.start()
        elif action == "complete":
            assert lease is not None
            await database.complete(lease)
        else:
            assert lease is not None
            await database.fail(lease)
    assert not database.rows(Evidence)
    assert all(row.status == "RUNNING" for row in database.rows(ToolCall))
    if action == "start":
        assert not database.audit_rows


@pytest.mark.parametrize("prior", ["RUNNING", "FAILED", "SUCCEEDED"])
async def test_actual_gateway_does_not_rerun_provider_for_existing_sdk_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prior: str,
) -> None:
    """実 Coordinator/Gateway/writer を接ぎ、再 Hook 後も Provider 呼出回数を増やさない。"""

    database = AuditDatabase(monkeypatch)
    lease = await database.start()
    if prior == "FAILED":
        await database.fail(lease)
    elif prior == "SUCCEEDED":
        await database.complete(lease)
    provider = CsvIssueProvider()
    registry = _registry(provider)
    context = replace(
        _context(tmp_path, registry), run_id=database.claimed.run_id,
        run_attempt_id=database.claimed.run_attempt_id, project_id=database.claimed.project_id,
        user_id=database.claimed.actor_id, tools=(database.invocation.tool,),
    )
    runtime = registry.build_gateway_runtime(context, audit_writer=database.writer)
    assert runtime.mcp.on_tool_authorized is not None
    await runtime.mcp.on_tool_authorized(
        database.invocation.tool.sdk_name, database.invocation.arguments,
        database.invocation.sdk_tool_use_id, str(database.invocation.agent_session_id),
    )
    response = await runtime.gateway.invoke_mcp(
        database.invocation.tool.sdk_name, database.invocation.arguments,
    )
    assert provider.calls == 0
    assert (response.get("is_error") is True) is (prior != "SUCCEEDED")


async def test_actual_gateway_publishes_once_and_replays_exact_committed_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """初回の実 Provider→audit commit と再 Hook の原結果読取を一つの経路で検証する。"""

    database = AuditDatabase(monkeypatch)
    provider = CsvIssueProvider()
    registry = _registry(provider)
    context = replace(
        _context(tmp_path, registry), run_id=database.claimed.run_id,
        run_attempt_id=database.claimed.run_attempt_id, project_id=database.claimed.project_id,
        user_id=database.claimed.actor_id, tools=(database.invocation.tool,),
    )
    runtime = registry.build_gateway_runtime(context, audit_writer=database.writer)
    assert runtime.mcp.on_tool_authorized is not None
    responses: list[dict[str, Any]] = []
    for _ in range(2):
        await runtime.mcp.on_tool_authorized(
            database.invocation.tool.sdk_name, database.invocation.arguments,
            database.invocation.sdk_tool_use_id, str(database.invocation.agent_session_id),
        )
        responses.append(await runtime.gateway.invoke_mcp(
            database.invocation.tool.sdk_name, database.invocation.arguments,
        ))
    assert responses[0] == responses[1] and "is_error" not in responses[0]
    assert provider.calls == 1
    assert len(database.rows(ToolCall)) == len(database.rows(Evidence)) == 1
    assert len(database.rows(PermissionDecision)) == 1


async def test_gateway_rejects_malformed_saved_success_without_provider_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """保存成功の Schema 欠損も失敗として扱い、新 Provider で修復/上書きしない。"""

    database = AuditDatabase(monkeypatch)
    await database.complete(await database.start())
    saved = database.rows(ToolCall)[0]
    del saved.result_json["issue"]
    original = deepcopy(saved.result_json)
    provider = CsvIssueProvider()
    registry = _registry(provider)
    context = replace(
        _context(tmp_path, registry), run_id=database.claimed.run_id,
        run_attempt_id=database.claimed.run_attempt_id, project_id=database.claimed.project_id,
        user_id=database.claimed.actor_id, tools=(database.invocation.tool,),
    )
    runtime = registry.build_gateway_runtime(context, audit_writer=database.writer)
    assert runtime.mcp.on_tool_authorized is not None
    await runtime.mcp.on_tool_authorized(
        database.invocation.tool.sdk_name, database.invocation.arguments,
        database.invocation.sdk_tool_use_id, str(database.invocation.agent_session_id),
    )
    response = await runtime.gateway.invoke_mcp(
        database.invocation.tool.sdk_name, database.invocation.arguments,
    )
    assert response["is_error"] is True and provider.calls == 0
    assert saved.result_json == original and saved.status == "SUCCEEDED"
