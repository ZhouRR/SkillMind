"""本番台帳/認可 SQL を合成 DB に接続し、PG の競争とは区別して検証する。"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.db.models import (
    AuthSession,
    OutboxMessage,
    SkillInterpretationCall,
    SkillInterpretationRequest,
    User,
)
from skillmind.skills.request_service import InterpretationRequestService
from tests.skills.skill_import_authorization_harness import ImportSession
from tests.skills.skill_lifecycle_harness import POSTGRESQL_DIALECT
from tests.skills.test_skill_publication_validation import PublicationResult


class RequestSession(ImportSession):
    """原会話と資産 rollback を再利用し、要求/呼出しの実 predicate を検証する。"""

    def ledger(self) -> InterpretationRequestService:
        """実 repository/authorizer は差替えず、接続だけを局部 seam にする。"""

        return InterpretationRequestService(cast(async_sessionmaker[AsyncSession], lambda: self))

    @property
    def requests(self) -> list[SkillInterpretationRequest]:
        """永続要求を明示型で取り出す。"""

        return [row for row in self.rows if isinstance(row, SkillInterpretationRequest)]

    @property
    def calls(self) -> list[SkillInterpretationCall]:
        """commit 済みと pending の呼出しを transaction seam の中で観測する。"""

        return [row for row in self.rows if isinstance(row, SkillInterpretationCall)]

    async def scalar(self, statement: Select[tuple[Any, ...]]) -> object:
        """参照資格と要求 PK の WHERE/lock/refresh を期待構造と比較する。"""

        entity = statement.column_descriptions[0]["entity"]
        parameters = statement.compile().params
        if entity is SkillInterpretationRequest:
            if "id_1" in parameters:
                expected = select(entity).where(entity.id == parameters["id_1"])
                matches = [row for row in self.requests if row.id == parameters["id_1"]]
            else:
                expected = select(entity).where(
                    entity.organization_id == parameters["organization_id_1"],
                    entity.execution_key == parameters["execution_key_1"],
                )
                matches = [
                    row
                    for row in self.requests
                    if row.organization_id == parameters["organization_id_1"]
                    and row.execution_key == parameters["execution_key_1"]
                ]
            if statement._for_update_arg is not None:
                expected = expected.with_for_update().execution_options(populate_existing=True)
                assert statement.get_execution_options()["populate_existing"] is True
                stage = "request-lock"
            else:
                stage = "request-read"
        elif entity is User:
            expected = (
                select(User)
                .where(
                    User.id == parameters["id_1"],
                    User.organization_id == parameters["organization_id_1"],
                )
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            matches = [
                user
                for user in self.users
                if user.id == parameters["id_1"]
                and user.organization_id == parameters["organization_id_1"]
            ]
            stage = "reference-user"
        elif entity is AuthSession:
            expected = (
                select(AuthSession)
                .where(
                    AuthSession.id == parameters["id_1"],
                    AuthSession.user_id == parameters["user_id_1"],
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            matches = [
                row
                for row in self.auth_sessions
                if row.id == parameters["id_1"] and row.user_id == parameters["user_id_1"]
            ]
            stage = "reference-session"
        else:
            return await super().scalar(statement)
        assert statement.compare(expected)
        self.queries.append(str(statement.compile(dialect=POSTGRESQL_DIALECT)))
        self.emit(stage)
        assert len(matches) <= 1
        return matches[0] if matches else None

    async def scalars(self, statement: Select[tuple[Any, ...]]) -> PublicationResult:
        """逐次呼出しの全件検索は要求 ID と ordinal 順序を検証する。"""

        if statement.column_descriptions[0]["entity"] is SkillInterpretationRequest:
            parameters = statement.compile().params
            before, limit = parameters["claimed_at_1"], parameters["param_1"]
            expected = (
                select(SkillInterpretationRequest.id)
                .where(
                    SkillInterpretationRequest.status == "RUNNING",
                    SkillInterpretationRequest.claimed_at <= before,
                )
                .order_by(SkillInterpretationRequest.claimed_at, SkillInterpretationRequest.id)
                .limit(limit)
            )
            assert statement.compare(expected)
            candidates = sorted(
                (
                    row
                    for row in self.requests
                    if row.status == "RUNNING"
                    and row.claimed_at is not None
                    and row.claimed_at <= before
                ),
                key=lambda row: (row.claimed_at, row.id),
            )[:limit]
            self.queries.append(str(statement.compile(dialect=POSTGRESQL_DIALECT)))
            self.emit("recovery-candidates")
            return PublicationResult(None, values=tuple(row.id for row in candidates))
        if statement.column_descriptions[0]["entity"] is not SkillInterpretationCall:
            return await super().scalars(statement)
        request_id = statement.compile().params["request_id_1"]
        expected = (
            select(SkillInterpretationCall)
            .where(SkillInterpretationCall.request_id == request_id)
            .order_by(SkillInterpretationCall.ordinal)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert statement.compare(expected)
        assert statement.get_execution_options()["populate_existing"] is True
        rows = tuple(
            sorted(
                (row for row in self.calls if row.request_id == request_id),
                key=lambda row: row.ordinal,
            )
        )
        self.queries.append(str(statement.compile(dialect=POSTGRESQL_DIALECT)))
        self.emit("calls")
        return PublicationResult(None, values=rows)

    def add(self, row: Any) -> None:
        """新しい台帳行だけ追加し、他の Skill 行は既存 FK 検査を使う。"""

        if not isinstance(
            row, (SkillInterpretationRequest, SkillInterpretationCall, OutboxMessage)
        ):
            super().add(row)
            return
        if isinstance(row, SkillInterpretationCall):
            assert row.request_id in self.persisted
            assert not any(
                item.request_id == row.request_id and item.ordinal == row.ordinal
                for item in self.calls
            )
        if isinstance(row, OutboxMessage):
            assert row.aggregate_id in self.persisted
        self.rows += (row,)
        self.pending.append(row)
        self.added.append(row)
