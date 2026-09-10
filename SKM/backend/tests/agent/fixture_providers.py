"""オフライン test だけが注入する、Credential を持たない合成 CSV/Git Provider。"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.text_window import select_line_window
from skillmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolProviderError,
)


def _sha256(value: bytes) -> str:
    """Evidence contract で使う prefix 付き SHA-256 を返す。"""

    return f"sha256:{hashlib.sha256(value).hexdigest()}"


class CsvFixtureIssueProvider:
    """固定 CSV snapshot から一つの Issue を読み出す read-only Provider。"""

    def __init__(self, csv_path: Path, *, source_namespace: str = "fixture") -> None:
        """Worker 起動時に fixture と Evidence namespace を固定する。"""

        self._path = csv_path.resolve(strict=True)
        self._source_namespace = _source_namespace(source_namespace)
        raw = self._path.read_bytes()
        with self._path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"id", "subject", "description", "status", "updated_at"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise ValueError("CSV fixture is missing required columns")
            rows = list(reader)
        self._rows: dict[str, tuple[int, dict[str, str]]] = {}
        for row_number, row in enumerate(rows, start=2):
            issue_id = row.get("id", "")
            if not issue_id or issue_id in self._rows:
                raise ValueError("CSV fixture contains an invalid or duplicate issue ID")
            self._rows[issue_id] = (row_number, row)
        self._snapshot_hash = _sha256(raw)

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """Bound fixture 内の指定 Issue と再現可能な row Evidence を返す。"""

        issue_ref = arguments.get("issue_ref")
        if not isinstance(issue_ref, str) or issue_ref not in self._rows:
            raise ToolProviderError("not_found", "Issue was not found", retryable=False)
        row_number, row = self._rows[issue_ref]
        standard = {"id", "subject", "description", "status", "updated_at"}
        fields = {key: value for key, value in row.items() if key not in standard}
        canonical = json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "csv",
                "issue": {
                    "id": row["id"],
                    "subject": row["subject"],
                    "description": row["description"] or None,
                    "status": row["status"] or None,
                    "updated_at": row["updated_at"] or None,
                    "fields": fields,
                    "extensions": {},
                },
                "warnings": [],
                "truncated": False,
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="issue",
                    source_uri=f"csv://{self._source_namespace}/issues/{issue_ref}",
                    source_locator={"sheet": "issues", "row": row_number},
                    content_hash=_sha256(canonical),
                    excerpt=row["subject"],
                    metadata={"snapshot_hash": self._snapshot_hash},
                ),
            ),
        )


class GitFixtureRepositoryProvider:
    """固定 revision の synthetic repository tree だけを読む Provider。"""

    def __init__(self, repository_root: Path, *, source_namespace: str = "fixture") -> None:
        """Fixture root、Evidence namespace と 40 桁 revision を起動時に固定する。"""

        self._root = repository_root.resolve(strict=True)
        self._source_namespace = _source_namespace(source_namespace)
        revision = (self._root / "revision.txt").read_text(encoding="utf-8").strip()
        if len(revision) != 40 or any(
            character not in "0123456789abcdef" for character in revision
        ):
            raise ValueError("Git fixture revision must be a 40 character lowercase SHA")
        self._revision = revision

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """Traversal を拒否し、指定 line 範囲と file hash を Evidence 付きで返す。"""

        revision = arguments.get("revision")
        raw_path = arguments.get("path")
        if revision != self._revision:
            raise ToolProviderError(
                "not_found", "Repository revision was not found", retryable=False
            )
        if not isinstance(raw_path, str):
            raise ToolProviderError(
                "invalid_request", "Repository path is invalid", retryable=False
            )
        relative = PurePosixPath(raw_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ToolProviderError(
                "invalid_request", "Repository path is invalid", retryable=False
            )
        path = (self._root / Path(*relative.parts)).resolve(strict=False)
        if not path.is_relative_to(self._root) or path.is_symlink() or not path.is_file():
            raise ToolProviderError("not_found", "Repository path was not found", retryable=False)
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolProviderError(
                "invalid_request", "Repository file is not UTF-8 text", retryable=False
            ) from error
        # 空 file を不正範囲として拒否する既存契約を保つため allow_empty は無効にする。
        window = select_line_window(content, arguments, subject="Repository", allow_empty=False)
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "git",
                "revision": self._revision,
                "path": relative.as_posix(),
                "content": window.content,
                "content_hash": _sha256(raw),
                "line_start": window.line_start,
                "line_end": window.line_end,
                "warnings": ["Content was truncated"] if window.truncated else [],
                "truncated": window.truncated,
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="source_code",
                    source_uri=(
                        f"git://{self._source_namespace}/{self._revision}/{relative.as_posix()}"
                    ),
                    source_locator={
                        "revision": self._revision,
                        "path": relative.as_posix(),
                        "line_start": window.line_start,
                        "line_end": window.line_end,
                    },
                    content_hash=_sha256(raw),
                    excerpt=window.content[:2_000],
                    metadata={"reproducibility": "fixed_fixture"},
                ),
            ),
        )


def _source_namespace(value: str) -> str:
    """Evidence URI に埋め込める固定 namespace だけを受理する。"""

    if (
        not value
        or len(value) > 64
        or not value[0].isalnum()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for character in value)
    ):
        raise ValueError("Fixture source namespace is invalid")
    return value
