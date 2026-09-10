"""実 Writer→Artifact repository→Result/checkpoint を同じ ORM 行の SQL fake で接続する。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList, Null
from sqlalchemy.sql.operators import and_, eq, in_op, is_not, or_

from projectmind.agent.result_validation import (
    PostgresArtifactLookup,
    PostgresEvidenceLookup,
    ResultValidationError,
    ResultValidator,
)
from projectmind.agent.tool_gateway import ToolRegistry
from projectmind.agent.workspace import WorkspaceManager
from projectmind.artifacts.domain import ArtifactIntegrityError
from projectmind.artifacts.repository import ArtifactRepository
from projectmind.db.models import Evidence, ToolCall
from projectmind.runs.repository_base import _RunRepositoryBase
from tests.agent.test_artifact_publication import (
    ObservedWriteProvider,
    _invoke,
    _publication,
    _runtime,
)
from tests.agent.test_gateway_invocation_ownership import OwnershipAuditWriter, _payload
from tests.agent.test_tool_audit_transactions import AuditDatabase


def _matches(rows: Mapping[str, Any], expression: Any) -> bool:
    """実 SELECT の限定した WHERE だけを評価し、未知 SQL は fixture の失敗にする。"""

    if isinstance(expression, BooleanClauseList):
        values = [_matches(rows, item) for item in expression.clauses]
        assert expression.operator in (and_, or_)
        return all(values) if expression.operator is and_ else any(values)
    assert isinstance(expression, BinaryExpression)
    source = rows[expression.left.table.name]
    key = expression.left.key
    assert isinstance(key, str)
    actual = getattr(source, key) if source is not None else None
    if isinstance(expression.right, Null):
        assert expression.operator is is_not
        return actual is not None
    assert isinstance(expression.right, BindParameter)
    if expression.operator is eq:
        return bool(actual == expression.right.value)
    assert expression.operator is in_op
    assert isinstance(expression.right.value, list | tuple | set | frozenset)
    return actual in expression.right.value


class ArtifactDatabase(AuditDatabase):
    """Tool audit の commit fake に Artifact SELECT を追加し、repository 自体は差し替えない。"""

    def __call__(self) -> MagicMock:
        """同じ保存済み行を Writer/Download/Result/checkpoint の全 session へ提供する。"""

        session = super().__call__()
        session.execute = AsyncMock(side_effect=self.execute)
        return session

    def _sources(self, evidence: Evidence) -> dict[str, Any]:
        """SQL の outer join を原 PK で評価し、外来 Tool/Attempt を勝手に修復しない。"""

        tool = next(
            (item for item in self.rows(ToolCall) if item.id == evidence.tool_call_id), None,
        )
        attempt = next((item for item in self.attempts
                        if tool is not None and item.id == tool.run_attempt_id), None)
        return {"evidence": evidence, "runs": self.run, "tool_calls": tool, "run_attempts": attempt}

    async def scalars(self, statement: Any) -> MagicMock:
        """EvidenceLookup の実 Run/ref WHERE を評価し、その他は既存 lock fake を使う。"""

        if statement.get_final_froms()[0].name != "evidence":
            return await super().scalars(statement)
        self.queries.append(statement)
        values = [item.evidence_ref for item in self.rows(Evidence)
                  if all(_matches(self._sources(item), expression)
                         for expression in statement._where_criteria)]
        result = MagicMock()
        result.__iter__.side_effect = lambda: iter(values)
        return result

    async def execute(self, statement: Any) -> MagicMock:
        """実 metadata/substring SELECT の列と limit を同一行に適用する局部 DB seam。"""

        self.queries.append(statement)
        assert "LEFT OUTER JOIN tool_calls" in str(statement)
        selected = list(statement.selected_columns)
        values = []
        for evidence in self.rows(Evidence):
            sources = self._sources(evidence)
            if evidence.run_id != self.run.id or not all(
                _matches(sources, expression) for expression in statement._where_criteria
            ):
                continue
            row: dict[str, Any] = {}
            for column in selected:
                key = column.key
                if key == "has_content":
                    row[key] = evidence.artifact_bytes is not None
                elif key == "content_size":
                    data = evidence.artifact_bytes
                    row[key] = len(data) if data is not None else None
                elif key == "content":
                    function = column.element
                    _, start, maximum = function.clauses
                    assert start.value == 1
                    data = evidence.artifact_bytes
                    row[key] = data[:maximum.value] if data is not None else None
                else:
                    source = column.element if hasattr(column, "element") else column
                    item = sources[source.table.name]
                    row[key] = getattr(item, source.key) if item is not None else None
            values.append(row)
        assert statement._limit_clause is not None
        values = values[:statement._limit_clause.value]
        result = MagicMock()
        result.mappings.return_value.all.return_value = values
        return result


def _validator(database: ArtifactDatabase) -> ResultValidator:
    """主/子の実 ResultValidator に本番の PostgreSQL lookup adapter を装配する。"""

    return ResultValidator(
        PostgresEvidenceLookup(database),  # type: ignore[arg-type]
        artifact_lookup=PostgresArtifactLookup(database),  # type: ignore[arg-type]
    )


async def _result(database: ArtifactDatabase, reference: str) -> Any:
    """Result の明示 Artifact convention へ原保存 ID を渡す。"""

    return await _validator(database).validate(
        run_id=database.run.id, schema={"type": "object"}, schema_ref="fixture://result",
        structured_output={"summary": "Saved report", "artifact_refs": [reference]},
    )


async def test_writer_repository_result_checkpoint_and_replay_share_original_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """quota も実 repository で確認し、同じ commit 済み byte を全 read consumer が検証する。"""

    database = ArtifactDatabase(monkeypatch)
    response, record = await _publication(database, tmp_path)
    lease = await database.start()
    returned = await database.writer.complete(
        lease, result=response, evidence=(record,), duration_ms=1,
    )
    assert returned == response and database.timeline[-1] == "commit"
    assert record.artifact_ref is not None
    session = database()
    repository = ArtifactRepository(session)
    listing = await repository.list_metadata(
        project_id=database.run.project_id, run_id=database.run.id,
    )
    content = await repository.get_content(
        project_id=database.run.project_id, run_id=database.run.id,
        artifact_ref=record.artifact_ref,
    )
    assert content is not None and listing == (content.metadata,)
    assert content.content == "Report 日本語".encode()
    result = await _result(database, record.artifact_ref)
    assert result.artifact_refs == frozenset({record.artifact_ref})
    assert result.validation["reference_checks"]["artifacts"] == "RUN_OWNERSHIP_AND_CONTENT"
    await _RunRepositoryBase(session)._validate_checkpoint_refs(
        database.run.id, {"artifact_refs": [record.artifact_ref]},
    )
    replay = await database.start()
    await database.writer.verify_dispatch(replay)
    assert replay.result == returned and not replay.is_new
    assert len(database.rows(Evidence)) == len(database.rows(ToolCall)) == 1
    # 実 WHERE が scope 違い/未知 ref を除外することを fake の無条件返却ではなく検証する。
    assert await repository.get_content(
        project_id=uuid4(), run_id=database.run.id, artifact_ref=record.artifact_ref,
    ) is None
    assert await repository.verified_refs(uuid4(), frozenset({record.artifact_ref})) == frozenset()


@pytest.mark.parametrize("corruption", ["bytes", "tool", "attempt", "receipt"])
async def test_all_consumers_reject_corruption_without_repair_or_provider_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, corruption: str,
) -> None:
    """一度確定した行の損傷を実 consumer 全てで拒否し、原 hash/result を書き換えない。"""

    database = ArtifactDatabase(monkeypatch)
    response, record = await _publication(database, tmp_path)
    lease = await database.start()
    await database.writer.complete(lease, result=response, evidence=(record,), duration_ms=1)
    evidence, tool = database.rows(Evidence)[0], database.rows(ToolCall)[0]
    assert record.artifact_ref is not None
    if corruption == "bytes":
        evidence.artifact_bytes = b"tampered"
    elif corruption == "tool":
        tool.provider = "foreign"
    elif corruption == "attempt":
        database.attempts.clear()
    else:
        tool.result_json = {**response, "artifact_refs": ["art_foreign"]}
    before = deepcopy(tool.result_json), evidence.content_hash
    session = database()
    with pytest.raises(ArtifactIntegrityError):
        await ArtifactRepository(session).get_content(
            project_id=database.run.project_id, run_id=database.run.id,
            artifact_ref=record.artifact_ref,
        )
    with pytest.raises(ResultValidationError):
        await _result(database, record.artifact_ref)
    with pytest.raises(ArtifactIntegrityError):
        await _RunRepositoryBase(session)._validate_checkpoint_refs(
            database.run.id, {"artifact_refs": [record.artifact_ref]},
        )
    assert (tool.result_json, evidence.content_hash) == before
    assert len(database.rows(Evidence)) == 1


async def test_real_gateway_replay_does_not_repeat_provider_and_overwrite_keeps_original_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """実 Gateway/Provider/Writer/read repo の組合せで、mutable file と保存 byte を区別する。"""

    database = ArtifactDatabase(monkeypatch)
    provider = ObservedWriteProvider()
    template, name = _runtime(tmp_path, OwnershipAuditWriter(), provider)
    workspace = WorkspaceManager((tmp_path / "verified-runs").resolve()).initialize(database.run.id)
    context = replace(
        template.gateway._context, run_id=database.run.id, run_attempt_id=database.attempt.id,
        project_id=database.run.project_id, user_id=database.claimed.actor_id,
        workspace=workspace,
    )
    registry = ToolRegistry(tuple(item.definition for item in template.gateway._bindings.values()))
    runtime = registry.build_gateway_runtime(context, audit_writer=database.writer)
    first = _payload(await _invoke(runtime, name))
    assert first["status"] == "success"
    replay = _payload(await _invoke(runtime, name))
    assert replay == first and provider.calls == 1
    replacement = _payload(await _invoke(runtime, name, content="Replacement", use_id="new-write"))
    assert replacement["status"] == "success" and provider.calls == 2
    assert replacement["artifact_refs"] != first["artifact_refs"]
    assert (context.workspace.root / "output/report.txt").read_bytes() == b"Replacement"
    content = await ArtifactRepository(database()).get_content(
        project_id=database.run.project_id, run_id=database.run.id,
        artifact_ref=first["artifact_refs"][0],
    )
    assert content is not None and content.content == "Original report 日本語".encode()
    assert len(database.rows(Evidence)) == len(database.rows(ToolCall)) == 2


async def test_unknown_commit_uses_original_published_bytes_without_another_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """commit 後の応答喪失でも保存事実を原 key から読み、欠落や再実行へ変換しない。"""

    database = ArtifactDatabase(monkeypatch)
    response, record = await _publication(database, tmp_path)
    lease = await database.start()
    database.commit_failure = "after"
    with pytest.raises(ConnectionError):
        await database.writer.complete(lease, result=response, evidence=(record,), duration_ms=1)
    replay = await database.start()
    await database.writer.verify_dispatch(replay)
    assert replay.result == response and not replay.is_new
    assert record.artifact_ref is not None
    result = await _result(database, record.artifact_ref)
    assert result.artifact_refs == frozenset({record.artifact_ref})
    assert len(database.rows(Evidence)) == 1
