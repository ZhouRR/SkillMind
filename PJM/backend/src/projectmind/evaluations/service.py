"""原会話と現在 Project 資格の一 transaction 内で評価を保存・確認する。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.sessions import UnauthorizedSessionError
from projectmind.evaluations.domain import (
    CreateEvaluationCommand,
    EvaluationError,
    InvalidEvaluationCommandError,
    StoredEvaluation,
    StoredEvaluationPage,
    StoredEvaluationSubmission,
)
from projectmind.evaluations.repository import EvaluationRepository
from projectmind.projects.domain import ProjectNotFoundError
from projectmind.projects.repository import ProjectRepository
from projectmind.runs.domain import RunNotFoundError
from projectmind.users.access import authorize_user_access, validate_user_access
from projectmind.users.domain import UserAccess
from projectmind.users.repository import UserRepository, authorization_failure_snapshot


class EvaluationService:
    """commit が正常終了した後だけ原要求の回执や履歴を呼出元へ返す。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """API と同じ DB factory を受け取り、別認証や自動再試行を作らない。"""

        self._session_factory = session_factory

    async def create(
        self,
        command: CreateEvaluationCommand,
        *,
        access: UserAccess,
    ) -> StoredEvaluation:
        """旧無キー追加にも原 cookie/CSRF と現在所属の再認証を適用する。"""

        command, access = _freeze_command(command, access)
        async with self._transaction(access, project_id=command.project_id, write=True) as repo:
            return await repo.create(command)

    async def submit(
        self,
        command: CreateEvaluationCommand,
        *,
        submission_key: UUID,
        result_id: UUID,
        access: UserAccess,
    ) -> StoredEvaluationSubmission:
        """可変提案値を最初の await 前に所有し、同一原内容の再送だけを重放する。"""

        command, access = _freeze_command(command, access)
        async with self._transaction(access, project_id=command.project_id, write=True) as repo:
            return await repo.submit(command, submission_key=submission_key, result_id=result_id)

    async def get_submission(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        submission_key: UUID,
        result_id: UUID,
        access: UserAccess,
    ) -> StoredEvaluationSubmission:
        """現在の同一 user の有効会話で原要求を読み、保存や再送をしない。"""

        access = deepcopy(access)
        async with self._transaction(access, project_id=project_id, write=False) as repo:
            return await repo.get_submission(
                project_id=project_id,
                run_id=run_id,
                user_id=access.actor.user_id,
                submission_key=submission_key,
                result_id=result_id,
            )

    async def list_for_run(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        access: UserAccess,
    ) -> tuple[StoredEvaluation, ...]:
        """旧履歴にも現在の読取資格を要求し、帰档済み Project は許可する。"""

        access = deepcopy(access)
        async with self._transaction(access, project_id=project_id, write=False) as repo:
            return await repo.list_for_run(project_id=project_id, run_id=run_id)

    async def list_page(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        access: UserAccess,
        limit: int = 20,
        after: UUID | None = None,
    ) -> StoredEvaluationPage:
        """有界ページと未命中にも、同じ原資格の最終確認を適用する。"""

        access = deepcopy(access)
        async with self._transaction(access, project_id=project_id, write=False) as repo:
            return await repo.list_page(
                project_id=project_id, run_id=run_id, limit=limit, after=after
            )

    @asynccontextmanager
    async def _transaction(
        self,
        access: UserAccess,
        *,
        project_id: UUID,
        write: bool,
    ) -> AsyncIterator[EvaluationRepository]:
        """既存の資格 lock と検証を再利用し、領域拒否と flush 後にも新時刻で検査する。"""

        validate_user_access(access)
        async with self._session_factory() as session, session.begin():
            users = await UserRepository(session).lock_users(
                access=access,
                target_id=None,
                include_target_sessions=False,
                read_only_actor=True,
            )
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=write)
            projects = ProjectRepository(session)
            try:
                project = await projects.lock_write_access(user=users.actor, project_id=project_id)
            except ProjectNotFoundError:
                authorize_user_access(
                    access, users, now=datetime.now(UTC), admin=False, write=write
                )
                raise

            def require_current_access() -> None:
                """保存値を読む前に会話の時刻を先判定し、失効時の Project 情報を隠す。"""

                authorize_user_access(
                    access, users, now=datetime.now(UTC), admin=False, write=write
                )
                if write:
                    projects.require_active_write_access(project)
                else:
                    projects.require_read_access(project)

            require_current_access()
            failure_snapshot = authorization_failure_snapshot(users)
            try:
                yield EvaluationRepository(session)
                require_current_access()
                await session.flush()
                require_current_access()
            except (EvaluationError, RunNotFoundError):
                require_current_access()
                raise
            except SQLAlchemyError:
                # failed flush 後は ORM が expire し得る。成功や新しい書込の認可には使わない。
                authorize_user_access(
                    access,
                    failure_snapshot,
                    now=datetime.now(UTC),
                    admin=False,
                    write=write,
                )
                raise
            # commit 応答未知・取消を成功/競合へ変換せず、別 transaction の補償を行わない。


def _freeze_command(
    command: CreateEvaluationCommand,
    access: UserAccess,
) -> tuple[CreateEvaluationCommand, UserAccess]:
    """HTTP 外 caller の actor 差替えと、await 中のネスト値変更を防ぐ。"""

    try:
        command, access = deepcopy(command), deepcopy(access)
    except (ValueError, TypeError, RecursionError) as error:
        raise InvalidEvaluationCommandError("Evaluation request is invalid") from error
    validate_user_access(access)
    if command.user_id != access.actor.user_id:
        raise UnauthorizedSessionError("Authentication is required")
    return command, access
