"""共有認証 SQL seam と実 Evaluation SQL を結ぶ。実 PG 競争・commit の証明ではない。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from sqlalchemy import Select, and_
from sqlalchemy.dialects import sqlite

from skillmind.db.models import Evaluation, Run, RunResult
from skillmind.evaluations.domain import (
    StoredEvaluation,
    StoredEvaluationPage,
    StoredEvaluationSubmission,
)
from skillmind.evaluations.service import EvaluationService
from tests.evaluations.test_evaluation_repository import _command, _result
from tests.runs.creation_authorization_harness import CreationAuthorizationHarness, require_where


class EvaluationDatabase(CreationAuthorizationHarness):
    """資格 repository を stub 化せず、Run/Result と評価行だけを追加する。"""

    def __init__(self) -> None:
        """現在 actor と一 Result を用意し、親の staged/commit/rollback hook を共有する。"""

        super().__init__()
        self.run = Run(id=uuid4(), project_id=self.project.id, status="SUCCEEDED")
        self.result = _result(self.run.id)
        self.run_present = self.result_present = True
        self.command = replace(
            _command(project_id=self.project.id, run_id=self.run.id),
            user_id=self.user.id,
        )
        self.submission_key = uuid4()
        self.evaluation_service = EvaluationService(self.session_factory)

    @property
    def evaluations(self) -> list[Evaluation]:
        """永続済み評価だけを返し、未 commit の行を成功履歴へ混ぜない。"""

        assert all(isinstance(row, Evaluation) for row in self.committed)
        return cast(list[Evaluation], self.committed)

    async def scalar(self, statement: Select[Any]) -> Any:
        """所有権と三元原要求の実 WHERE を評価し、別 Result の存在を補完しない。"""

        entity = statement.column_descriptions[0]["entity"]
        if entity not in (Run, RunResult, Evaluation):
            return await super().scalar(statement)
        _, params = self.query(statement)
        if entity is Run:
            assert statement.column_descriptions[0]["expr"] is Run.id
            assert statement.whereclause is not None and statement.whereclause.compare(
                and_(
                    Run.id == params["id_1"],
                    Run.project_id == params["project_id_1"],
                )
            )
            return (
                self.run.id
                if self.run_present
                and self.run.id == params["id_1"]
                and self.run.project_id == params["project_id_1"]
                else None
            )
        if entity is RunResult:
            require_where(statement, RunResult, RunResult.run_id == params["run_id_1"])
            self.step("result")
            return (
                self.result
                if self.result_present and self.result.run_id == params["run_id_1"]
                else None
            )
        self.step("lookup")
        rows = self.select_evaluations(statement)
        assert len(rows) <= 1
        return rows[0] if rows else None

    async def scalars(self, statement: Select[Any]) -> MagicMock:
        """資格集合は親の厳密 scope 検査、評価 page は実 SQL の order/limit を通す。"""

        if statement.column_descriptions[0]["entity"] is not Evaluation:
            return await super().scalars(statement)
        self.query(statement)
        self.step("page")
        result = MagicMock()
        result.all.return_value = self.select_evaluations(statement)
        return result

    def select_evaluations(self, statement: Select[Any]) -> list[Evaluation]:
        """SQLite は SELECT 条件だけの局部 seam。PG の lock と保存保証を代替しない。"""

        rows = [row for row in [*self.committed, *self.staged] if isinstance(row, Evaluation)]
        with closing(sqlite3.connect(":memory:")) as database:
            database.execute(
                "CREATE TABLE evaluations (id TEXT, result_id TEXT, user_id TEXT, "
                "submission_key TEXT, created_at TEXT)"
            )
            database.executemany(
                "INSERT INTO evaluations VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        row.id.hex,
                        row.result_id.hex,
                        row.user_id.hex,
                        row.submission_key.hex if row.submission_key else None,
                        row.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
                        if isinstance(row.created_at, datetime)
                        else "invalid",
                    )
                    for row in rows
                ],
            )
            sql = str(
                statement.with_only_columns(Evaluation.id).compile(
                    dialect=sqlite.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            identities = [UUID(hex=row[0]) for row in database.execute(sql).fetchall()]
        by_id = {row.id: row for row in rows}
        return [by_id[identity] for identity in identities]

    def add(self, row: Any) -> None:
        """保存候補は Evaluation 一行のみ許可し、flush で公開しない。"""

        assert self.active and isinstance(row, Evaluation)
        assert row.result_id == self.result.id and row.user_id == self.access.actor.user_id
        self.staged.append(row)

    async def submit(self) -> StoredEvaluationSubmission:
        """新しい原要求入口を実 service に渡す。"""

        return await self.evaluation_service.submit(
            self.command,
            submission_key=self.submission_key,
            result_id=self.result.id,
            access=self.access,
        )

    async def create(self) -> StoredEvaluation:
        """旧無キー追加入口も同じ資格と transaction を使う。"""

        return await self.evaluation_service.create(self.command, access=self.access)

    async def get_submission(self) -> StoredEvaluationSubmission:
        """原 Result/key の読取を実 service に渡す。"""

        return await self.evaluation_service.get_submission(
            project_id=self.project.id,
            run_id=self.run.id,
            submission_key=self.submission_key,
            result_id=self.result.id,
            access=self.access,
        )

    async def list_page(
        self, *, limit: int = 20, after: UUID | None = None
    ) -> StoredEvaluationPage:
        """有界 page を SQL 順序のまま取得する。"""

        return await self.evaluation_service.list_page(
            project_id=self.project.id,
            run_id=self.run.id,
            access=self.access,
            limit=limit,
            after=after,
        )
