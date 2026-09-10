"""入力準備の凍結 claim と、ファイルから独立した回执 port をテストで組み立てる。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from skillmind.agent.repository_source import RepositoryBindingRef
from skillmind.core.hashing import sha256_hex
from skillmind.documents.snapshot import DOCUMENT_PROVIDER, DocumentSnapshot
from skillmind.runs.domain import ClaimedRun, LeaseValidationError
from skillmind.runs.input_snapshot import (
    InputFileSeal,
    InputSnapshotError,
    InputSnapshotRecord,
    InputSnapshotStatus,
    input_source_checksum,
    input_tree_checksum,
    parse_input_files,
)

TEST_BINDING_CHECKSUM = f"sha256:{sha256_hex('frozen test repository binding')}"


def input_claim(
    *,
    project_id: UUID,
    run_id: UUID,
    snapshots: Sequence[DocumentSnapshot] = (),
    repository_bindings: Mapping[str, RepositoryBindingRef] | None = None,
) -> ClaimedRun:
    """初回準備の前に選択を固定し、再訪時の引数から再署名しない。"""

    sources = {
        snapshot.requirement_key: {
            "provider": DOCUMENT_PROVIDER,
            "capability": "document.read/v1",
            "resource_kind": "document",
            "access": "read",
            "document_snapshot": snapshot.to_json(),
        }
        for snapshot in snapshots
    }
    for key, binding in (repository_bindings or {}).items():
        sources[key] = {
            "provider": binding.provider,
            "capability": "repository.read/v1",
            "resource_kind": "repository",
            "access": "read",
            "binding_id": str(binding.binding_id),
            "integration_id": str(binding.integration_id),
            "binding_checksum": TEST_BINDING_CHECKSUM,
        }
    return ClaimedRun(
        run_id=run_id,
        run_attempt_id=uuid4(),
        run_segment_id=uuid4(),
        project_id=project_id,
        actor_id=uuid4(),
        attempt_no=1,
        lease_token="input-test-lease",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        row_version=2,
        input_json={},
        task_snapshot_json={},
        permission_snapshot_json={},
        selected_sources_json=sources,
        limits_snapshot_json={},
    )


class MemoryInputSnapshots:
    """DB の代役だけを担い、物理 file や manifest から信頼を生成しない fake。"""

    def __init__(self, claimed: ClaimedRun) -> None:
        """認可の正本を呼出し側とは別に保存し、引数改変の自動承認を防ぐ。"""

        self.current_claim = deepcopy(claimed)
        self._project_id = claimed.project_id
        self._run_id = claimed.run_id
        self._source_checksum = self._checksum(claimed)
        self.record: InputSnapshotRecord | None = None
        self.cancelled = False
        self.begin_calls = 0
        self.complete_calls = 0

    async def begin(self, claimed: ClaimedRun) -> tuple[InputSnapshotRecord, bool]:
        """一つの Run に一つだけ世代を発行し、再訪では元の回执を返す。"""

        self.begin_calls += 1
        self._authorize(claimed)
        if self.record is not None:
            return self.record, False
        self.record = InputSnapshotRecord(
            snapshot_id=uuid4(),
            project_id=self._project_id,
            run_id=self._run_id,
            prepared_by_attempt_id=claimed.run_attempt_id,
            source_checksum=self._source_checksum,
            status=InputSnapshotStatus.PREPARING,
            files=(),
            tree_checksum=None,
            completed_at=None,
        )
        return self.record, True

    async def complete(
        self, claimed: ClaimedRun, *, snapshot_id: UUID, files: tuple[InputFileSeal, ...]
    ) -> InputSnapshotRecord:
        """元の準備者・現在の lease・同じ内容だけを、一度完成記録へ進める。"""

        self.complete_calls += 1
        self._authorize(claimed)
        record = self.record
        if (
            record is None
            or record.snapshot_id != snapshot_id
            or record.prepared_by_attempt_id != claimed.run_attempt_id
        ):
            raise InputSnapshotError("Input preparation generation does not match")
        files = parse_input_files([item.to_json() for item in files])
        checksum = input_tree_checksum(files)
        if record.status is InputSnapshotStatus.READY:
            if record.files != files or record.tree_checksum != checksum:
                raise InputSnapshotError("Completed input receipts cannot be replaced")
            return record
        self.record = replace(
            record,
            status=InputSnapshotStatus.READY,
            files=files,
            tree_checksum=checksum,
            completed_at=datetime.now(UTC),
        )
        return self.record

    def _authorize(self, claimed: ClaimedRun) -> None:
        """freeze と現在の実行権を別々に検査し、取消や接管後の旧所有者を拒否する。"""

        if self.cancelled:
            raise InputSnapshotError("Input preparation was cancelled")
        current = self.current_claim
        if (
            claimed.project_id != self._project_id
            or claimed.run_id != self._run_id
            or claimed.run_attempt_id != current.run_attempt_id
            or claimed.run_segment_id != current.run_segment_id
            or claimed.lease_token != current.lease_token
            or current.lease_expires_at <= datetime.now(UTC)
        ):
            raise LeaseValidationError("Input test lease is no longer current")
        if self._checksum(claimed) != self._source_checksum:
            raise InputSnapshotError("Input preparation sources do not match the Run")

    @staticmethod
    def _checksum(claimed: ClaimedRun) -> str:
        """ファイルではなく凍結 Run source から authority の摘要を作る。"""

        return input_source_checksum(
            project_id=claimed.project_id,
            run_id=claimed.run_id,
            sources=claimed.selected_sources_json,
        )
