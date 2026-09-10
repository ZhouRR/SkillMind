"""実 Artifact SQL と戻り値検証を fake session で確認する。実 DB の競争証明ではない。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote
from uuid import uuid4

import pytest
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.artifacts.domain import (
    MAX_ARTIFACT_BYTES,
    MAX_RUN_ARTIFACT_BYTES,
    MAX_RUN_ARTIFACTS,
    ArtifactIntegrityError,
)
from projectmind.artifacts.repository import ArtifactRepository
from projectmind.core.hashing import sha256_hex
from tests.artifacts.test_domain import metadata


def saved_row(content: bytes = b"original") -> dict[str, Any]:
    """本来の一 Tool/一 Evidence/一 Artifact の保存関連を組み立てる。"""

    item = metadata(content)
    return {
        "artifact_ref": item.artifact_ref, "project_id": item.project_id, "run_id": item.run_id,
        "tool_call_id": item.tool_call_id, "evidence_ref": item.evidence_ref,
        "artifact_path": item.path, "artifact_size": item.size_bytes,
        "artifact_mime_type": item.mime_type, "content_hash": item.checksum,
        "created_at": item.created_at, "has_content": True, "content_size": len(content),
        "tool_identity": item.tool_call_id, "tool_run_id": item.run_id,
        "attempt_run_id": item.run_id, "capability_version": "workspace.write/v2",
        "provider": "workspace", "status": "SUCCEEDED", "content": content,
        "tool_name": "mcp__projectmind__workspace_write_v2", "integration_id": None,
        "evidence_type": "workspace-write", "snapshot_uri": None,
        "source_uri": f"workspace://runs/{item.run_id}/{quote(item.path, safe='/')}",
        "source_locator": {"path": item.path, "bytes": item.size_bytes},
        "metadata_json": {"scope": "run-workspace", "read_only": False},
        "result_json": {
            "status": "success", "provider": "workspace", "path": item.path,
            "content_hash": item.checksum, "bytes_written": item.size_bytes,
            "artifact_refs": [item.artifact_ref], "evidence_refs": [item.evidence_ref],
            "created": True, "warnings": [],
        },
    }


def session_for(rows: list[dict[str, Any]]) -> AsyncMock:
    """DB 接続を作らず、SQL と同一 snapshot の mapping だけを返す。"""

    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    session.execute.return_value = result
    return session


async def read(
    action: str, session: AsyncMock, row: dict[str, Any],
) -> Any:
    """三つの実 repository 入口へ同じ原 identity を渡す。"""

    repository = ArtifactRepository(session)
    if action == "list":
        return await repository.list_metadata(project_id=row["project_id"], run_id=row["run_id"])
    if action == "get":
        return await repository.get_content(
            project_id=row["project_id"], run_id=row["run_id"], artifact_ref=row["artifact_ref"],
        )
    return await repository.verified_refs(row["run_id"], frozenset({row["artifact_ref"]}))


@pytest.mark.parametrize("action", ["list", "get", "refs"])
async def test_original_success_is_read_without_current_run_state_or_workspace(action: str) -> None:
    """終態/旧 Attempt も原帰属で読め、現在 file や最新 binding を参照しない。"""

    row = saved_row()
    session = session_for([row])
    result = await read(action, session, row)
    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=cast(type[Dialect], dialect)())
    sql = str(compiled)
    assert "runs.id = evidence.run_id" in sql
    assert "LEFT OUTER JOIN tool_calls ON tool_calls.id = evidence.tool_call_id" in sql
    assert "LEFT OUTER JOIN run_attempts ON run_attempts.id = tool_calls.run_attempt_id" in sql
    assert row["run_id"] in compiled.params.values()
    assert "evidence.run_id =" in sql
    assert "runs.status" not in sql and "run_attempts.status" not in sql
    assert "workspace://" not in sql
    assert "UPDATE " not in sql and "FOR UPDATE" not in sql
    if action == "list":
        assert "substring" not in sql and MAX_RUN_ARTIFACTS + 1 in compiled.params.values()
        assert result[0].checksum == row["content_hash"]
        assert not {"content", "metadata_json", "source_locator", "snapshot_uri"} & asdict(
            result[0]
        ).keys()
    else:
        assert "SUBSTRING(evidence.artifact_bytes FROM" in sql
        assert MAX_ARTIFACT_BYTES + 1 in compiled.params.values()
        if action == "get":
            assert result.content == b"original"
        else:
            assert result == frozenset({row["artifact_ref"]})
    if action != "refs":
        assert row["project_id"] in compiled.params.values() and "runs.project_id =" in sql
    session.execute.assert_awaited_once()


@pytest.mark.parametrize("action", ["list", "get", "refs"])
@pytest.mark.parametrize("changes", [
    {"tool_identity": None}, {"tool_run_id": uuid4()}, {"attempt_run_id": None},
    {"attempt_run_id": uuid4()}, {"status": "RUNNING"}, {"status": "FAILED"},
    {"capability_version": "workspace.write/v1"}, {"provider": "foreign"},
    {"has_content": False}, {"content_size": -1}, {"content_size": True},
    {"artifact_size": None}, {"artifact_mime_type": None}, {"artifact_path": None},
    {"result_json": None},
    {"tool_name": "mcp__projectmind__workspace_write_v1"}, {"integration_id": uuid4()},
    {"evidence_type": "workspace-file"}, {"source_uri": "workspace://runs/foreign/output/file"},
    {"source_locator": {"path": "output/file", "bytes": 7}},
    {"source_locator": {"path": "output/report.txt", "bytes": 7, "extra": True}},
    {"metadata_json": {"scope": "run-workspace", "read_only": True}},
    {"metadata_json": {"scope": "run-workspace", "read_only": 0}},
    {"metadata_json": {"scope": "foreign", "read_only": False}},
    {"snapshot_uri": "workspace://runs/foreign/output/file"},
])
async def test_broken_binding_is_integrity_error_not_absence(
    action: str, changes: dict[str, Any],
) -> None:
    """欠落/外来 Tool/旧 capability/半 binding を空一覧や未存在へ落とさない。"""

    row = saved_row()
    row.update(changes)
    with pytest.raises(ArtifactIntegrityError, match=r"^Artifact does not match"):
        await read(action, session_for([row]), row)


@pytest.mark.parametrize("action", ["list", "get", "refs"])
@pytest.mark.parametrize("changes", [
    {"status": "error"}, {"provider": "foreign"}, {"path": "output/replacement"},
    {"content_hash": "sha256:" + "0" * 64}, {"bytes_written": True}, {"bytes_written": 9},
    {"artifact_refs": ["art_foreign"]}, {"artifact_refs": []},
    {"artifact_refs": ["art_original", "art_foreign"]}, {"evidence_refs": ["ev_foreign"]},
])
async def test_original_tool_response_must_match_exact_saved_asset(
    action: str, changes: dict[str, Any],
) -> None:
    """合法同 Run の他参照や現在 response を借りて原対応を偽装させない。"""

    row = saved_row()
    row["result_json"].update(changes)
    with pytest.raises(ArtifactIntegrityError):
        await read(action, session_for([row]), row)


@pytest.mark.parametrize("action", ["get", "refs"])
@pytest.mark.parametrize("content", [None, b"originaL", b"", b"a" * (MAX_ARTIFACT_BYTES + 1)])
async def test_content_requires_same_actual_bytes_size_and_hash(action: str, content: Any) -> None:
    """DB substring で返る実 byte を検証し、同長の改竄も拒否する。"""

    row = saved_row()
    row["content"] = content
    with pytest.raises(ArtifactIntegrityError):
        await read(action, session_for([row]), row)


@pytest.mark.parametrize("action", ["get", "refs"])
async def test_binary_content_is_rejected_even_with_matching_saved_hash(action: str) -> None:
    """size/hash の自己整合だけでは現 UTF-8 producer の保存値として受理しない。"""

    row = saved_row()
    row.update(content=b"\xff", content_size=1, artifact_size=1,
               content_hash="sha256:" + sha256_hex(b"\xff"))
    row["result_json"].update(bytes_written=1, content_hash=row["content_hash"])
    row["source_locator"]["bytes"] = 1
    with pytest.raises(ArtifactIntegrityError):
        await read(action, session_for([row]), row)


@pytest.mark.parametrize("action", ["get", "refs"])
@pytest.mark.parametrize("content", [b"", b"a" * MAX_ARTIFACT_BYTES])
async def test_empty_and_exact_limit_bytes_are_valid(action: str, content: bytes) -> None:
    """0 byte と上限ぴったりを oversized と取り違えない。"""

    row = saved_row(content)
    assert await read(action, session_for([row]), row) is not None


@pytest.mark.parametrize("action", ["list", "get", "refs"])
async def test_unknown_identity_is_not_synthesized(action: str) -> None:
    """未知/旧 NULL の参照に現在 workspace や架空の空 Artifact を補わない。"""

    row = saved_row()
    result = await read(action, session_for([]), row)
    assert result == {"list": (), "get": None, "refs": frozenset()}[action]


async def test_empty_ref_set_performs_no_io_and_excessive_refs_fail_before_io() -> None:
    """入力集合も有界にし、空参照のために DB を開かない。"""

    session = session_for([])
    repository = ArtifactRepository(session)
    assert await repository.verified_refs(uuid4(), frozenset()) == frozenset()
    with pytest.raises(ArtifactIntegrityError):
        await repository.verified_refs(uuid4(), frozenset(f"art_{index}" for index in range(101)))
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("count,size", [(101, 0), (11, MAX_ARTIFACT_BYTES)])
async def test_list_rejects_excessive_count_or_cumulative_bytes(count: int, size: int) -> None:
    """LIMIT や byte 合計の違反を正常な部分一覧へ切り捨てない。"""

    original = saved_row(b"a" * size)
    rows = []
    for index in range(count):
        row = deepcopy(original)
        row["artifact_ref"] = f"art_{index}"
        row["result_json"]["artifact_refs"] = [row["artifact_ref"]]
        rows.append(row)
    with pytest.raises(ArtifactIntegrityError):
        await read("list", session_for(rows), original)
    assert count > MAX_RUN_ARTIFACTS or count * size > MAX_RUN_ARTIFACT_BYTES


@pytest.mark.parametrize("action", ["list", "get", "refs"])
async def test_duplicate_saved_reference_is_not_first_row_wins(action: str) -> None:
    """唯一 index の損傷を最初の行だけの成功に変えない。"""

    row = saved_row()
    with pytest.raises(ArtifactIntegrityError):
        await read(action, session_for([row, deepcopy(row)]), row)


async def test_list_detects_partial_binding_and_does_not_select_private_payload() -> None:
    """ref のない半 binding も SQL 対象にし、正文そのものは一覧 SELECT に含めない。"""

    row = saved_row()
    row["artifact_ref"] = None
    session = session_for([row])
    with pytest.raises(ArtifactIntegrityError):
        await read("list", session, row)
    statement = session.execute.call_args.args[0]
    sql = str(statement.compile(dialect=cast(type[Dialect], dialect)()))
    for column in (
        "artifact_ref", "artifact_bytes", "artifact_size", "artifact_mime_type", "artifact_path",
    ):
        assert f"evidence.{column} IS NOT NULL" in sql
    assert "artifact_bytes" not in statement.selected_columns
