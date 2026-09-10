"""原版比較、変更の実体、原 credential の再認証を fake transaction で検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from skillmind.auth.sessions import UnauthorizedSessionError
from skillmind.projects.domain import (
    MAX_PROJECT_VERSION,
    ProjectKeyConflictError,
    ProjectStatus,
    ProjectVersionConflictError,
    ProjectVersionExhaustedError,
    UpdateProjectCommand,
    validate_project_version,
)
from skillmind.projects.repository import ProjectRepository
from tests.projects.project_harness import Members

_POSTGRESQL = MigrationContext.configure(dialect_name="postgresql").dialect


async def mutate(members: Members, operation: str, *, expected: int = 1) -> object:
    """同じ原会話と対象で各 CRUD 用例を呼び、HTTP の暗黙補完を使用しない。"""

    if operation == "create":
        return await members.service.create_project(
            access=members.access,
            key="created",
            name="Created",
            description="",
            settings={},
            retention_days=90,
        )
    if operation == "update":
        return await members.service.update_project(
            access=members.access,
            project_id=members.project.id,
            command=UpdateProjectCommand(expected_row_version=expected, name=members.project.name),
        )
    method = getattr(members.service, f"{operation}_project")
    return await method(
        access=members.access,
        project_id=members.project.id,
        expected_row_version=expected,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "archive", "unarchive", "delete"])
async def test_stale_version_precedes_noop_and_delete_guards(
    members: Members,
    operation: str,
) -> None:
    """同じ目標値/削除前提不足でも旧版は先に衝突し、参照 lookup や書込へ進まない。"""

    members.project.row_version = 2
    members.project.status = "ARCHIVED" if operation == "archive" else "ACTIVE"
    members.session.scalar.side_effect = [members.project]
    original_time = members.project.updated_at
    with pytest.raises(ProjectVersionConflictError):
        await mutate(members, operation)
    assert members.project.row_version == 2 and members.project.updated_at == original_time
    assert members.session.scalar.await_count == 1
    members.session.add.assert_not_called()
    members.session.execute.assert_not_called()
    members.session.delete.assert_not_called()


@pytest.mark.parametrize("version", [1, MAX_PROJECT_VERSION])
@pytest.mark.parametrize("operation", ["update", "archive", "unarchive"])
def test_same_version_noop_never_changes_version_or_time(
    members: Members,
    version: int,
    operation: str,
) -> None:
    """最大版でも no-op は成功し、空 description/settings も正しい既存値として比較する。"""

    project = members.project
    project.row_version = version
    now, before = datetime.now(UTC), project.updated_at
    repository = ProjectRepository(members.session)
    if operation == "update":
        result = repository.update(
            project=project,
            command=UpdateProjectCommand(version, name=project.name, description="", settings={}),
            now=now,
        )
    else:
        target = ProjectStatus.ARCHIVED if operation == "archive" else ProjectStatus.ACTIVE
        project.status = target.value
        result = repository.set_status(
            project=project,
            status=target,
            expected_row_version=version,
            now=now,
        )
    assert result.row_version == version and result.updated_at == before
    assert project.updated_at == before


@pytest.mark.parametrize("operation", ["update", "archive", "unarchive"])
def test_changed_metadata_or_status_advances_once_and_max_refuses_before_mutation(
    members: Members,
    operation: str,
) -> None:
    """一操作で複数 metadata を変えても一版だけ増え、上限ではどの field も書き換えない。"""

    project = members.project
    repository = ProjectRepository(members.session)
    now = datetime.now(UTC)
    if operation == "update":
        result = repository.update(
            project=project,
            command=UpdateProjectCommand(1, name="Changed", description="new"),
            now=now,
        )
        assert result.name == "Changed" and result.description == "new"
    else:
        target = ProjectStatus.ARCHIVED if operation == "archive" else ProjectStatus.ACTIVE
        project.status = "ACTIVE" if operation == "archive" else "ARCHIVED"
        result = repository.set_status(
            project=project,
            status=target,
            expected_row_version=1,
            now=now,
        )
    assert result.row_version == 2 and result.updated_at == now
    project.row_version = MAX_PROJECT_VERSION
    before = (project.name, project.description, project.status, project.updated_at)
    with pytest.raises(ProjectVersionExhaustedError):
        if operation == "update":
            repository.update(
                project=project,
                command=UpdateProjectCommand(MAX_PROJECT_VERSION, name="Another"),
                now=datetime.now(UTC),
            )
        else:
            opposite = (
                ProjectStatus.ACTIVE if project.status == "ARCHIVED" else ProjectStatus.ARCHIVED
            )
            repository.set_status(
                project=project,
                status=opposite,
                expected_row_version=MAX_PROJECT_VERSION,
                now=datetime.now(UTC),
            )
    assert (project.name, project.description, project.status, project.updated_at) == before


def test_settings_comparison_retains_json_boolean_identity(members: Members) -> None:
    """Python の True == 1 によって JSON 型の変更を no-op と誤判定しない。"""

    members.project.settings_json = {"value": True}
    result = ProjectRepository(members.session).update(
        project=members.project,
        command=UpdateProjectCommand(1, settings={"value": 1}),
        now=datetime.now(UTC),
    )
    assert result.row_version == 2 and type(result.settings["value"]) is int


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "archive", "unarchive", "delete"])
@pytest.mark.parametrize("stage", ["initial_lock", "project_wait", "final_flush"])
async def test_all_project_writes_recheck_original_session_in_their_transaction(
    members: Members,
    operation: str,
    stage: str,
) -> None:
    """各待機境界で原会話が失効したら、同一 transaction が成功で終了しない。"""

    if operation == "unarchive":
        members.project.status = "ARCHIVED"
    if operation == "delete":
        members.project.status = "ARCHIVED"
    query_results = (
        [None] if operation == "create" else [members.project, 0, False, False, False, False, False]
    )
    if stage == "initial_lock":
        members.locked.current_session.revoked_at = datetime.now(UTC)
        members.session.scalar.side_effect = query_results
    elif stage == "project_wait":

        async def waited(statement: object) -> object:
            """資源/一意性 query の待機完了時点で失効を見せる。"""

            members.locked.current_session.idle_expires_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
            return query_results.pop(0)

        members.session.scalar.side_effect = waited
    else:
        members.session.scalar.side_effect = query_results

        async def flushed() -> None:
            """flush が返った後の新時刻で absolute 期限超過を検出させる。"""

            members.locked.current_session.absolute_expires_at = datetime.now(UTC) - timedelta(
                seconds=1
            )

        members.session.flush.side_effect = flushed
    with pytest.raises(UnauthorizedSessionError) as raised:
        await mutate(members, operation)
    assert members.transaction.__aexit__.call_args.args[:2] == (
        UnauthorizedSessionError,
        raised.value,
    )
    members.session.commit.assert_not_called()
    if stage == "initial_lock":
        members.session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_uses_common_user_gate_before_project_and_retains_max_version(
    members: Members,
) -> None:
    """Org/User/原Session 共通 lock の後に Project を取り、最大版の安全な削除も許可する。"""

    order: list[str] = []

    async def locked(**kwargs: object) -> object:
        """共有ロック取得順は UserRepository の SQL 回帰でも確認する。"""

        order.append("users_and_session")
        return members.locked

    members.lock_users.side_effect = locked
    members.project.status = "ARCHIVED"
    members.project.row_version = MAX_PROJECT_VERSION
    responses = [members.project, 0, False, False, False, False, False]

    async def scalar(statement: object) -> object:
        """Project lock が共通 lock より前に呼ばれないことを記録する。"""

        order.append("project_or_reference")
        return responses.pop(0)

    members.session.scalar.side_effect = scalar
    await mutate(members, "delete", expected=MAX_PROJECT_VERSION)
    assert order[0:2] == ["users_and_session", "project_or_reference"]
    query = members.session.scalar.call_args_list[0].args[0].compile(dialect=_POSTGRESQL)
    assert "FOR UPDATE" in str(query)
    assert set(query.params.values()) == {members.project.id, members.access.actor.organization_id}
    members.session.delete.assert_awaited_once_with(members.project)


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", ["uq_projects_organization_key", "other_constraint", None])
async def test_create_maps_only_the_known_project_key_constraint(
    members: Members,
    constraint: str | None,
) -> None:
    """FK や別の DB 障害は key 重複と偽らず、同じ transaction の失敗として戻す。"""

    original = RuntimeError("test database detail")
    original.diag = SimpleNamespace(constraint_name=constraint)  # type: ignore[attr-defined]
    error = IntegrityError("test insert", {}, original)
    members.session.scalar = AsyncMock(return_value=None)
    members.session.flush = AsyncMock(side_effect=error)
    expected = (
        ProjectKeyConflictError if constraint == "uq_projects_organization_key" else IntegrityError
    )
    with pytest.raises(expected):
        await mutate(members, "create")
    members.session.commit.assert_not_called()


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "1", None, MAX_PROJECT_VERSION + 1])
def test_internal_version_validation_is_strict(value: object) -> None:
    """HTTP 以外の caller にも bool/float/範囲外の期待版を許可しない。"""

    with pytest.raises(ValueError):
        validate_project_version(value)
