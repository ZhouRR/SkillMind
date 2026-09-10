"""Run input の独立した可信回执を、workspace 内の manifest から分離する。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.runs.domain import ClaimedRun


class InputSnapshotError(RuntimeError):
    """原状を残して不完全・改変済み副本の利用を止める。"""


class InputSnapshotStatus(StrEnum):
    """準備の認領と、検証済み全 input の公開を区別する。"""

    PREPARING = "PREPARING"
    READY = "READY"


@dataclass(frozen=True, slots=True)
class InputFileSeal:
    """可信回执に保存する一 file の位置・実 byte 数・hash。"""

    path: str
    size: int
    checksum: str

    def __post_init__(self) -> None:
        """DB に保存する値も相対 path と厳密な hash/byte 数に制限する。"""

        if (
            not self.path
            or "\\" in self.path
            or not self.path.isprintable()
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
            or isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or self.size < 0
            or re.fullmatch(r"sha256:[a-f0-9]{64}", self.checksum) is None
        ):
            raise InputSnapshotError("Input file receipt is invalid")

    def to_json(self) -> dict[str, Any]:
        """保存・比較の形を一つに固定する。"""

        return {"path": self.path, "size": self.size, "checksum": self.checksum}


def parse_input_files(value: Any) -> tuple[InputFileSeal, ...]:
    """未知 field、重複・衝突 path、非正規順序を欠損扱いで通さない。"""

    if not isinstance(value, list):
        raise InputSnapshotError("Input file receipts are invalid")
    files: list[InputFileSeal] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "checksum"}
            or not isinstance(item["path"], str)
            or not isinstance(item["checksum"], str)
        ):
            raise InputSnapshotError("Input file receipt is invalid")
        files.append(InputFileSeal(item["path"], item["size"], item["checksum"]))
    paths = [item.path for item in files]
    if paths != sorted(set(paths)):
        raise InputSnapshotError("Input file receipts have duplicate or unordered paths")
    seen = set(paths)
    if any(
        "/".join(path.split("/")[:index]) in seen
        for path in paths
        for index in range(1, len(path.split("/")))
    ):
        raise InputSnapshotError("Input file receipts have conflicting paths")
    return tuple(files)


def input_tree_checksum(files: Sequence[InputFileSeal]) -> str:
    """全 root の原文・変換物・生成物・manifest を同じ完成摘要に含める。"""

    return f"sha256:{sha256_hex(canonical_json([item.to_json() for item in files]))}"


def input_source_checksum(*, project_id: UUID, run_id: UUID, sources: Mapping[str, Any]) -> str:
    """作成時の凍結来源へ回执を結び、live directory から由来を再推論しない。"""

    return f"sha256:{
        sha256_hex(
            canonical_json(
                {
                    'version': 'v1',
                    'project_id': str(project_id),
                    'run_id': str(run_id),
                    'sources': sources,
                }
            )
        )
    }"


@dataclass(frozen=True, slots=True)
class InputSnapshotRecord:
    """Run 全体で一つの準備世代と各 root の file receipt を保持する不変投影。"""

    snapshot_id: UUID
    project_id: UUID
    run_id: UUID
    prepared_by_attempt_id: UUID
    source_checksum: str
    status: InputSnapshotStatus
    files: tuple[InputFileSeal, ...]
    tree_checksum: str | None
    completed_at: datetime | None


class InputSnapshotStore(Protocol):
    """準備の認領と公開を現在の Run lease で fencing する永続 port。"""

    async def begin(self, claimed: ClaimedRun) -> tuple[InputSnapshotRecord, bool]:
        """初回だけ準備世代を認領し、既存世代は改変せず返す。bool は今回の認領有無。"""

        ...

    async def complete(
        self, claimed: ClaimedRun, *, snapshot_id: UUID, files: tuple[InputFileSeal, ...]
    ) -> InputSnapshotRecord:
        """全 file の公開後だけ READY にし、取消・失効 lease・世代違いを拒否する。"""

        ...
