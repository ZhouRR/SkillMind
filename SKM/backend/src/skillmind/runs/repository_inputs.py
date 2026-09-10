"""Run の input 準備回执を、既存 aggregate lock/lease 境界で永続化する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.db.models import Run, RunInputSnapshot
from skillmind.runs.domain import ClaimedRun, LeaseValidationError, RunStatus
from skillmind.runs.input_snapshot import (
    InputFileSeal,
    InputSnapshotError,
    InputSnapshotRecord,
    InputSnapshotStatus,
    input_source_checksum,
    input_tree_checksum,
    parse_input_files,
)
from skillmind.runs.repository_base import _RunRepositoryBase


class RunInputRepository(_RunRepositoryBase):
    """Run に一つだけの準備世代を保存し、READY の証拠を上書きしない。"""

    async def begin(self, claimed: ClaimedRun) -> tuple[InputSnapshotRecord, bool]:
        """認領前に Run → Segment → Attempt の lock と現在 lease を検証する。"""

        run = await self._authorize(claimed)
        existing = await self._get(run.id)
        if existing is not None:
            return self._record(existing, run), False
        row = RunInputSnapshot(
            id=uuid4(),
            run_id=run.id,
            project_id=run.project_id,
            prepared_by_attempt_id=claimed.run_attempt_id,
            source_checksum=input_source_checksum(
                project_id=run.project_id, run_id=run.id, sources=run.selected_sources_json
            ),
            status=InputSnapshotStatus.PREPARING.value,
            files_json=[],
            tree_checksum=None,
            total_files=0,
            total_bytes=0,
            created_at=datetime.now(UTC),
            completed_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return self._record(row, run), True

    async def complete(
        self, claimed: ClaimedRun, *, snapshot_id: UUID, files: tuple[InputFileSeal, ...]
    ) -> InputSnapshotRecord:
        """現在 lease の所有者だけが、自分の認領世代を一度完成させる。"""

        run = await self._authorize(claimed)
        row = await self._get(run.id)
        if (
            row is None
            or row.id != snapshot_id
            or row.prepared_by_attempt_id != claimed.run_attempt_id
        ):
            raise InputSnapshotError("Input preparation generation does not match")
        record = self._record(row, run)
        serialized = [item.to_json() for item in files]
        files = parse_input_files(serialized)
        checksum = input_tree_checksum(files)
        if record.status is InputSnapshotStatus.READY:
            if record.files != files or record.tree_checksum != checksum:
                raise InputSnapshotError("Completed input receipts cannot be replaced")
            return record
        row.files_json = serialized
        row.tree_checksum = checksum
        row.total_files = len(files)
        row.total_bytes = sum(item.size for item in files)
        row.status = InputSnapshotStatus.READY.value
        row.completed_at = datetime.now(UTC)
        await self._session.flush()
        return self._record(row, run)

    async def _get(self, run_id: UUID) -> RunInputSnapshot | None:
        """Run aggregate lock の後に、その Run 唯一の回执を取得する。"""

        return (
            await self._session.scalars(
                select(RunInputSnapshot).where(RunInputSnapshot.run_id == run_id)
            )
        ).one_or_none()

    async def confirm_completed(
        self, claimed: ClaimedRun, *, snapshot_id: UUID, files: tuple[InputFileSeal, ...]
    ) -> InputSnapshotRecord:
        """結果不明な commit は原世代の読取だけで確認し、新しい認領や補签をしない。"""

        run = await self._authorize(claimed)
        row = await self._get(run.id)
        if (
            row is None
            or row.id != snapshot_id
            or row.prepared_by_attempt_id != claimed.run_attempt_id
        ):
            raise InputSnapshotError("Input completion could not be confirmed for this generation")
        record = self._record(row, run)
        if (
            record.status is not InputSnapshotStatus.READY
            or record.files != files
            or record.tree_checksum != input_tree_checksum(files)
        ):
            raise InputSnapshotError("Input completion has no matching committed receipt")
        return record

    async def _authorize(self, claimed: ClaimedRun) -> Run:
        """期限・取消・Project と凍結来源を確認し、旧 Worker の完成宣言を阻止する。"""

        run, _, attempt = await self._lock_claimed_execution(claimed)
        if await self.is_cancellation_requested(run.id):
            raise InputSnapshotError("Input preparation was cancelled")
        # lock 待機前の時刻を使うと、待機中に期限が切れた lease が通ってしまう。
        self._validate_claimed_lease(attempt, claimed, now=datetime.now(UTC))
        if run.project_id != claimed.project_id or run.status not in {
            RunStatus.PREPARING.value,
            RunStatus.RUNNING.value,
        }:
            raise LeaseValidationError("Run is not available for input preparation")
        if input_source_checksum(
            project_id=run.project_id, run_id=run.id, sources=run.selected_sources_json
        ) != input_source_checksum(
            project_id=claimed.project_id,
            run_id=claimed.run_id,
            sources=claimed.selected_sources_json,
        ):
            raise InputSnapshotError("Input preparation sources do not match the Run")
        return run

    @staticmethod
    def _record(row: RunInputSnapshot, run: Run) -> InputSnapshotRecord:
        """保存値を検証し、欠損・未知形式を workspace から補填しない。"""

        if row.project_id != run.project_id or row.source_checksum != input_source_checksum(
            project_id=run.project_id, run_id=run.id, sources=run.selected_sources_json
        ):
            raise InputSnapshotError("Input receipt does not belong to the frozen Run")
        try:
            status = InputSnapshotStatus(row.status)
        except ValueError as error:
            raise InputSnapshotError("Input receipt status is invalid") from error
        files = parse_input_files(row.files_json)
        if row.total_files != len(files) or row.total_bytes != sum(item.size for item in files):
            raise InputSnapshotError("Input receipt totals are invalid")
        if status is InputSnapshotStatus.READY:
            if row.completed_at is None or row.tree_checksum != input_tree_checksum(files):
                raise InputSnapshotError("Completed input receipt is invalid")
        elif files or row.completed_at is not None or row.tree_checksum is not None:
            raise InputSnapshotError("Incomplete input receipt is invalid")
        return InputSnapshotRecord(
            snapshot_id=row.id,
            project_id=row.project_id,
            run_id=row.run_id,
            prepared_by_attempt_id=row.prepared_by_attempt_id,
            source_checksum=row.source_checksum,
            status=status,
            files=files,
            tree_checksum=row.tree_checksum,
            completed_at=row.completed_at,
        )


class PostgresInputSnapshotStore:
    """file I/O の前後だけ短い transaction を開く Worker-side adapter。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """外部取得中に DB lock を保持しないよう transaction factory のみ受け取る。"""

        self._session_factory = session_factory

    async def begin(self, claimed: ClaimedRun) -> tuple[InputSnapshotRecord, bool]:
        """準備記録を commit してから file の生成を許可する。"""

        async with self._session_factory() as session, session.begin():
            return await RunInputRepository(session).begin(claimed)

    async def complete(
        self, claimed: ClaimedRun, *, snapshot_id: UUID, files: tuple[InputFileSeal, ...]
    ) -> InputSnapshotRecord:
        """commit の応答を失った場合も、別 transaction で同じ完成事実だけを確認する。"""

        completed: InputSnapshotRecord | None = None
        try:
            async with self._session_factory() as session, session.begin():
                completed = await RunInputRepository(session).complete(
                    claimed, snapshot_id=snapshot_id, files=files
                )
        except (DBAPIError, TimeoutError, ConnectionError):
            # repository が返る前の検証/flush 失敗は commit 確認へ昇格させない。CancelledError
            # も捕捉せず、準備 deadline・Worker 停止の後に新たな DB 作業を始めない。
            if completed is None:
                raise
            # 失敗した session の identity map を再利用しない。読戻しにも新しい lock 時刻の
            # lease/取消検証を適用し、PREPARING・別世代・不一致なら原状を保存して閉じる。
            async with self._session_factory() as session, session.begin():
                return await RunInputRepository(session).confirm_completed(
                    claimed, snapshot_id=snapshot_id, files=files
                )
        return completed
