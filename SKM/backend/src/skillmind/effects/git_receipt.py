"""原 Git Effect の commit を照合する只読 port。push/再送は提供しない。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from skillmind.agent.repository_client import GitWritableRepositoryClient, RepositoryCredential
from skillmind.core.hashing import canonical_json, sha256_hex


@dataclass(frozen=True, slots=True)
class GitCommitCommand:
    """元批准から再構築する、単一 branch と全文 hash の照合条件。"""

    effect_id: UUID
    project_id: UUID
    run_id: UUID
    integration_id: UUID
    branch: str
    base_revision: str
    message: str
    files: tuple[tuple[str, str | None], ...]

    @property
    def request_checksum(self) -> str:
        """接続先は別 snapshot、原 commit 内容はこの checksum へ束縛する。"""
        return "sha256:" + sha256_hex(
            canonical_json(
                {
                    "effect_id": str(self.effect_id),
                    "project_id": str(self.project_id), "run_id": str(self.run_id),
                    "integration_id": str(self.integration_id),
                    "branch": self.branch,
                    "base_revision": self.base_revision,
                    "message": self.message,
                    "files": self.files,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class GitCommitReceipt:
    """元 Effect が遠端に存在した観測。平台 APPLIED への状態変更ではない。"""

    effect_id: UUID
    request_checksum: str
    commit_revision: str


class GitCommitConflictError(ValueError):
    """対象 branch は存在するが原 Effect の commit と一致しない。"""


def git_effect_commit_message(
    *, display: Any, proposal_ref: str, effect_id: UUID, fingerprint: str
) -> str:
    """書込と核対が同じ原 Effect trailer を生成する。"""
    headline = display if isinstance(display, str) and display else "Apply approved change"
    return (
        f"{headline}\n\nSkillmind-Proposal: {proposal_ref}\n"
        f"Skillmind-Effect: {effect_id}\nSkillmind-Request: {fingerprint}\n"
    )


class GitCommitReader:
    """遠端を隔離 checkout へ読み、原 commit identity・親・全変更集合・本文を検証する。"""

    def __init__(self, client: GitWritableRepositoryClient) -> None:
        """Git transport は共有するが、公開 port は lookup のみとする。"""
        self._client = client

    async def lookup(
        self,
        config: Mapping[str, Any],
        credential: str,
        command: GitCommitCommand,
        *,
        authorize: Callable[[], Awaitable[None]],
    ) -> GitCommitReceipt | None:
        """Branch 先頭が原 commit の場合だけ確認し、不在・移動から再送可否を推定しない。"""
        await authorize()
        async with self._client.open_writable(
            uri=str(config["repository_uri"]),
            revision=command.base_revision,
            credential=RepositoryCredential.from_material(credential),
        ) as session:
            head = await session.remote_branch_head(command.branch)
            if head is None or head == command.base_revision:
                await authorize()
                return None
            head = await session.fetch_branch(command.branch)
            if not await session.matches_commit_identity(
                head, message=command.message, paths=tuple(path for path, _ in command.files)
            ):
                raise GitCommitConflictError("Original Git commit was not found at branch head")
            for path, content in command.files:
                actual = await session.read_file_at(head, path, max_bytes=1_048_576)
                expected = None if content is None else content.encode("utf-8")
                if actual != expected:
                    raise GitCommitConflictError("Original Git content differs")
            await authorize()
            return GitCommitReceipt(command.effect_id, command.request_checksum, head)


def validate_git_receipt(command: GitCommitCommand, receipt: GitCommitReceipt) -> None:
    """返却回执を原要求と有効な full commit ID に限定する。"""
    if (
        receipt.effect_id != command.effect_id
        or receipt.request_checksum != command.request_checksum
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", receipt.commit_revision) is None
    ):
        raise ValueError("Original Git receipt is invalid")
