"""入力回执の SQL 境界と、commit 応答喪失時の一回限りの読戻しを検証する。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import Select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import Run, RunAttempt, RunInputSnapshot, RunSegment
from skillmind.runs import repository_inputs as module
from skillmind.runs.domain import ClaimedRun, LeaseValidationError, RunStatus
from skillmind.runs.input_snapshot import (
    InputFileSeal,
    InputSnapshotError,
    InputSnapshotStatus,
    input_source_checksum,
    input_tree_checksum,
)
from skillmind.runs.repository_inputs import PostgresInputSnapshotStore, RunInputRepository
from tests.runs.test_execution_gates import execution_rows, row_result

FILES = (InputFileSeal("documents/a.md", 3, "sha256:" + "a" * 64),)


@dataclass
class _Database:
    """実 query/validator を通すための行集合。transaction の原子性は模倣しない。"""

    claimed: ClaimedRun
    run: Run
    segment: RunSegment
    attempt: RunAttempt
    receipt: RunInputSnapshot | None
    session: MagicMock


def _database(*, ready: bool = True, exists: bool = True) -> _Database:
    """同じ Run の実 model を返す query fake を作り、保存値の検証を迂回しない。"""

    claimed, run, segment, attempt = execution_rows()
    run.selected_sources_json = deepcopy(claimed.selected_sources_json)
    receipt = (
        RunInputSnapshot(
            id=uuid4(),
            run_id=run.id,
            project_id=run.project_id,
            prepared_by_attempt_id=claimed.run_attempt_id,
            source_checksum=input_source_checksum(
                project_id=run.project_id, run_id=run.id, sources=run.selected_sources_json
            ),
            status="READY" if ready else "PREPARING",
            files_json=[item.to_json() for item in FILES] if ready else [],
            tree_checksum=input_tree_checksum(FILES) if ready else None,
            total_files=len(FILES) if ready else 0,
            total_bytes=sum(item.size for item in FILES) if ready else 0,
            created_at=datetime.now(UTC),
            completed_at=datetime.now(UTC) if ready else None,
        )
        if exists
        else None
    )
    session = MagicMock(spec=AsyncSession)
    database = _Database(claimed, run, segment, attempt, receipt, session)

    async def read_rows(statement: Select[Any]) -> MagicMock:
        """SQL が求めた table の行だけを返し、lock 順序を assertion 可能にする。"""

        table = statement.get_final_froms()[0].name
        return row_result(
            {
                "runs": run,
                "run_segments": segment,
                "run_attempts": attempt,
                "run_input_snapshots": database.receipt,
            }[table]
        )

    def save(row: RunInputSnapshot) -> None:
        """新規認領行を保存し、同じ Run の次回 begin から観測可能にする。"""

        assert database.receipt is None
        database.receipt = row

    session.scalars = AsyncMock(side_effect=read_rows)
    session.scalar = AsyncMock(return_value=None)
    session.add.side_effect = save
    return database


async def test_begin_creates_only_one_generation_and_complete_is_immutable() -> None:
    """複数 begin と完成再送が同じ row に収束し、別内容の完成は拒否される。"""

    database = _database(exists=False)
    repository = RunInputRepository(database.session)
    pending, created = await repository.begin(database.claimed)
    assert created and pending.status is InputSnapshotStatus.PREPARING
    assert await repository.begin(database.claimed) == (pending, False)
    completed = await repository.complete(
        database.claimed, snapshot_id=pending.snapshot_id, files=FILES
    )
    assert completed.status is InputSnapshotStatus.READY
    assert completed.tree_checksum == input_tree_checksum(FILES)
    assert (
        await repository.complete(database.claimed, snapshot_id=pending.snapshot_id, files=FILES)
        == completed
    )
    with pytest.raises(InputSnapshotError, match="cannot be replaced"):
        await repository.complete(database.claimed, snapshot_id=pending.snapshot_id, files=())
    assert database.session.add.call_count == 1
    assert database.session.flush.await_count == 2


async def test_confirmation_is_read_only_and_locks_the_execution_before_the_receipt() -> None:
    """確定済み回执の確認も現在 lease を要求し、認領・flush・更新を行わない。"""

    database = _database()
    assert database.receipt is not None
    result = await RunInputRepository(database.session).confirm_completed(
        database.claimed, snapshot_id=database.receipt.id, files=FILES
    )
    assert result.status is InputSnapshotStatus.READY and result.files == FILES
    statements = [call.args[0] for call in database.session.scalars.await_args_list]
    assert [item.get_final_froms()[0].name for item in statements] == [
        "runs",
        "run_segments",
        "run_attempts",
        "run_input_snapshots",
    ]
    assert all("FOR UPDATE" in str(item) for item in statements[:3])
    database.session.add.assert_not_called()
    database.session.flush.assert_not_awaited()


@pytest.mark.parametrize(
    "invalid",
    [
        "cancel",
        "lease",
        "project",
        "run_state",
        "source",
        "missing",
        "preparing",
        "generation",
        "preparer",
        "files",
        "tree",
        "totals",
    ],
)
async def test_confirmation_rejects_any_unproven_completion(invalid: str) -> None:
    """完成の推測や補签をせず、原世代と現在実行権の両方が証明された場合だけ返す。"""

    database = _database(ready=invalid != "preparing")
    assert database.receipt is not None
    snapshot_id = database.receipt.id
    files = FILES
    if invalid == "cancel":
        database.session.scalar.return_value = uuid4()
    elif invalid == "lease":
        database.attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif invalid == "project":
        database.run.project_id = uuid4()
    elif invalid == "run_state":
        database.run.status = RunStatus.WAITING_FOR_INPUT.value
    elif invalid == "source":
        database.run.selected_sources_json = {"different": {"provider": "unselected"}}
    elif invalid == "missing":
        database.receipt = None
    elif invalid == "generation":
        snapshot_id = uuid4()
    elif invalid == "preparer":
        database.receipt.prepared_by_attempt_id = uuid4()
    elif invalid == "files":
        files = ()
    elif invalid == "tree":
        database.receipt.tree_checksum = "sha256:" + "b" * 64
    elif invalid == "totals":
        database.receipt.total_bytes += 1
    error = (
        LeaseValidationError if invalid in {"lease", "project", "run_state"} else InputSnapshotError
    )
    with pytest.raises(error):
        await RunInputRepository(database.session).confirm_completed(
            database.claimed, snapshot_id=snapshot_id, files=files
        )
    database.session.add.assert_not_called()
    database.session.flush.assert_not_awaited()


async def test_confirmation_checks_time_after_waiting_for_cancellation_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lock 前に有効でも、DB 待機後に期限切れなら完成を返さない。"""

    database = _database()
    assert database.receipt is not None
    after_wait = database.attempt.lease_expires_at + timedelta(seconds=1)
    clock = Mock(wraps=datetime)
    clock.now.return_value = after_wait
    monkeypatch.setattr(module, "datetime", clock)
    with pytest.raises(LeaseValidationError):
        await RunInputRepository(database.session).confirm_completed(
            database.claimed, snapshot_id=database.receipt.id, files=FILES
        )
    assert database.session.scalar.await_count == 1
    assert database.session.scalars.await_count == 3


async def test_completed_generation_can_be_read_by_a_new_attempt_but_not_resigned() -> None:
    """接管は完成済み入力を再利用できるが、他 Attempt の完成操作は引き継がない。"""

    database = _database()
    assert database.receipt is not None
    database.receipt.prepared_by_attempt_id = uuid4()
    repository = RunInputRepository(database.session)
    ready, created = await repository.begin(database.claimed)
    assert ready.status is InputSnapshotStatus.READY and not created
    with pytest.raises(InputSnapshotError, match="generation"):
        await repository.complete(database.claimed, snapshot_id=ready.snapshot_id, files=FILES)
    database.session.flush.assert_not_awaited()


def _session(*, commit_error: BaseException | None = None) -> MagicMock:
    """context 出口を commit 境界として制御し、実 DB の動作保証とは区別する。"""

    session = MagicMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    transaction = MagicMock()
    transaction.__aexit__.side_effect = commit_error
    transaction.__aexit__.return_value = False
    session.begin.return_value = transaction
    return session


@pytest.mark.parametrize("failure", ["connection", "timeout", "driver"])
async def test_lost_commit_response_confirms_once_in_a_new_session(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """応答喪失後は original identity/files で読戻し、complete や begin を再送しない。"""

    database = _database()
    assert database.receipt is not None
    record = RunInputRepository._record(database.receipt, database.run)
    error = {
        "connection": ConnectionError("commit response lost"),
        "timeout": TimeoutError("commit response timed out"),
        "driver": DBAPIError(
            None, None, ConnectionError("connection lost"), connection_invalidated=True
        ),
    }[failure]
    first, second = _session(commit_error=error), _session()
    factory = Mock(side_effect=[first, second])
    writing, reading = Mock(), Mock()
    writing.complete = AsyncMock(return_value=record)
    reading.confirm_completed = AsyncMock(return_value=record)
    repositories = Mock(side_effect=[writing, reading])
    monkeypatch.setattr(module, "RunInputRepository", repositories)
    result = await PostgresInputSnapshotStore(factory).complete(
        database.claimed, snapshot_id=record.snapshot_id, files=FILES
    )
    assert result == record and factory.call_count == 2
    assert [call.args[0] for call in repositories.call_args_list] == [first, second]
    writing.complete.assert_awaited_once_with(
        database.claimed, snapshot_id=record.snapshot_id, files=FILES
    )
    reading.confirm_completed.assert_awaited_once_with(
        database.claimed, snapshot_id=record.snapshot_id, files=FILES
    )
    writing.begin.assert_not_called()
    reading.complete.assert_not_called()


@pytest.mark.parametrize("phase", ["before_commit", "cancellation", "confirmation_failure"])
async def test_unknown_completion_does_not_retry_writes_or_suppress_cancellation(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """検証/flush 失敗と外部取消を救済扱いせず、読戻し不能時も一回で閉じる。"""

    database = _database()
    assert database.receipt is not None
    record = RunInputRepository._record(database.receipt, database.run)
    error: BaseException = (
        asyncio.CancelledError() if phase == "cancellation" else ConnectionError()
    )
    first = _session(commit_error=None if phase == "before_commit" else error)
    factory = Mock(side_effect=[first, _session()])
    writing, reading = Mock(), Mock()
    writing.complete = AsyncMock(
        return_value=record, side_effect=error if phase == "before_commit" else None
    )
    reading.confirm_completed = AsyncMock(side_effect=InputSnapshotError("not confirmed"))
    monkeypatch.setattr(module, "RunInputRepository", Mock(side_effect=[writing, reading]))
    expected = InputSnapshotError if phase == "confirmation_failure" else type(error)
    with pytest.raises(expected):
        await PostgresInputSnapshotStore(factory).complete(
            database.claimed, snapshot_id=record.snapshot_id, files=FILES
        )
    assert factory.call_count == (2 if phase == "confirmation_failure" else 1)
    assert writing.complete.await_count == 1
    reading.complete.assert_not_called()
