"""四 Skill 管理操作の資格・原監査・局部 transaction 復元を実用例で検証する。

SQL/待機と rollback は合成であり、実 PostgreSQL 競争や物理 commit の証明ではない。
認証/領域エラーを mock せず、現在の ORM 値を共有本番 authorizer に判断させる。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import ORMExecuteState, Session, make_transient_to_detached

from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.db.models import ProjectSkillVersion, RuntimeManifest, SkillSource, SkillVersion
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.skills.domain import (
    PublishedTaskNotFoundError,
    SkillVersionDeleteBlockedError,
    SkillVersionEnablementConflictError,
    SkillVersionEnablementNotFoundError,
    SkillVersionNotFoundError,
    SkillVersionTransitionError,
    StoredProjectSkillVersion,
    StoredSkillVersion,
)
from skillmind.users.domain import UserAdministrationDeniedError
from tests.skills.skill_lifecycle_harness import (
    OPERATIONS,
    REFERENCE_MODELS,
    LifecycleSession,
    Operation,
)
from tests.skills.test_skill_publication_authorization import EXPIRY, NOW, Clock

PATHS = ("new", "replay")


async def prepare(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, path: str
) -> LifecycleSession:
    """原行を実用例で変更してから再送を準備し、存在しない再送成功を補造しない。"""
    Clock.current = NOW
    monkeypatch.setattr("skillmind.skills.service.datetime", Clock)
    session = LifecycleSession(operation)
    session.auth_session.idle_expires_at = EXPIRY
    session.auth_session.absolute_expires_at = EXPIRY
    if path == "replay":
        await session.operate()
        session.reset_observations()
        session.mutations.clear()
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("path", PATHS)
async def test_all_operations_require_current_admin_and_preserve_original_audit(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, path: str
) -> None:
    """同版操作の再送で元 publisher/有効化監査を更新せず、削除の再送は 404 にする。"""
    session = await prepare(monkeypatch, operation, path)
    before = session.frozen_values()
    original_manifest = deepcopy(session.manifest.manifest_json)
    publisher = (session.version.published_by, session.version.published_at)
    enabled = (session.binding.enabled_by, session.binding.enabled_at)
    if operation == "delete" and path == "replay":
        with pytest.raises(SkillVersionNotFoundError):
            await session.operate()
        assert session.frozen_values() == before
        assert session.commits == 0
    else:
        result = await session.operate()
        assert session.commits == 1 and session.rollbacks == 0
        assert session.timeline[-2:] == ["flush:1", "commit:1"]
        if operation == "deprecate":
            assert isinstance(result, StoredSkillVersion)
            assert result.status == "DEPRECATED"
        elif operation == "delete":
            assert result is None
            assert session.mutations == [
                "delete-bindings",
                "delete-RuntimeManifest",
                "delete-SkillVersion",
            ]
            assert session.bindings == []
            assert all(not isinstance(row, RuntimeManifest | SkillVersion) for row in session.rows)
        else:
            assert isinstance(result, StoredProjectSkillVersion)
            assert result.project_id == session.project.id
            assert result.skill_version.skill_version_id == session.version.id
            assert session.timeline.index("project:1") < session.timeline.index(
                "get-SkillVersion:1"
            )
            if operation == "disable" or path == "replay":
                assert (session.binding.enabled_by, session.binding.enabled_at) == enabled
        if path == "replay":
            assert session.frozen_values() == before
    assert session.timeline[:3] == ["organization:1", "select-User:1", "select-AuthSession:1"]
    assert session.manifest.manifest_json == original_manifest
    assert (session.version.published_by, session.version.published_at) == publisher
    version_load = next(load for load in session.loads if load[0] is SkillVersion)
    assert version_load[2] == {"with_for_update": True, "populate_existing": True}
    assert not any("project_members" in query for query in session.queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("denial", ["revoked", "disabled", "role", "csrf", "user", "missing"])
async def test_original_persisted_credential_is_required_even_for_noop_or_deleted_target(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, path: str, denial: str
) -> None:
    """入口の ADMIN 表示ではなく、現在の原会話/資格で四操作の初回と再送を拒否する。"""
    session = await prepare(monkeypatch, operation, path)
    expected: type[Exception] = UnauthorizedSessionError
    if denial == "revoked":
        session.auth_session.revoked_at = NOW
    elif denial == "disabled":
        session.user.status = "DISABLED"
    elif denial == "role":
        session.user.system_role = "USER"
    elif denial == "csrf":
        session.access = replace(session.access, csrf_token="")
        expected = CsrfRejectedError
    elif denial == "user":
        session.user.system_role = session.auth_session.system_role_at_login = "USER"
        expected = UserAdministrationDeniedError
    else:
        session.auth_sessions.clear()
    before = session.frozen_values()
    with pytest.raises(expected):
        await session.operate()
    assert session.frozen_values() == before
    assert session.commits == 0
    assert not any(point.startswith("get-") or point == "project:1" for point in session.timeline)
    assert session.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["enable", "disable"])
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("denial", ["missing", "foreign", "archived"])
async def test_project_is_locked_and_reauthorized_without_admin_membership(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, path: str, denial: str
) -> None:
    """API 入口が通っても、業務 transaction の現在 Project を再読取して拒否する。"""
    session = await prepare(monkeypatch, operation, path)
    if denial == "missing":
        session.project_present = False
    elif denial == "foreign":
        session.project.organization_id = uuid4()
    else:
        session.project.status = "ARCHIVED"
    before = session.frozen_values()
    with pytest.raises(ProjectArchivedError if denial == "archived" else ProjectNotFoundError):
        await session.operate()
    assert "project:1" in session.timeline
    assert "get-SkillVersion:1" not in session.timeline
    assert session.frozen_values() == before and session.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("case", ["missing-version", "foreign-skill", "foreign-source"])
async def test_skill_ownership_remains_exact_before_any_lifecycle_mutation(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, case: str
) -> None:
    """同じ UUID を要求しても別組織の Skill/Source を操作できない。"""
    session = await prepare(monkeypatch, operation, "new")
    if case == "missing-version":
        session.missing.add(SkillVersion)
    elif case == "foreign-skill":
        session.skill.organization_id = uuid4()
    else:
        session.source.organization_id = uuid4()
    before = session.frozen_values()
    with pytest.raises(SkillVersionNotFoundError):
        await session.operate()
    assert session.frozen_values() == before and session.commits == 0
    assert session.mutations == []


WAIT_CASES = [
    (operation, path, stage)
    for operation in OPERATIONS
    for path in PATHS
    for stage in (
        "organization:1",
        "select-User:1",
        "select-AuthSession:1",
        "get-SkillVersion:1",
        *(
            ()
            if operation == "delete" and path == "replay"
            else (
                "get-SkillSource:1",
                "get-Skill:1",
                "select-RuntimeManifest:1",
                "flush:1",
            )
        ),
        *(
            ("project:1", "project-identity:1", "binding:1")
            if operation in {"enable", "disable"}
            else ()
        ),
        *(
            (
                "references-RunSkillSnapshot:1",
                "references-ChangeProposal:1",
                "references-SkillCompositionItem:1",
                "references-TaskSchedule:1",
                "references-TaskScheduleOccurrence:1",
                "references-FrontendModuleVersion:1",
                "delete-bindings:1",
                "delete-RuntimeManifest:1",
                "delete-SkillVersion:1",
            )
            if operation == "delete" and path == "new"
            else ()
        ),
    )
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,path,stage", WAIT_CASES)
@pytest.mark.parametrize("expiry_kind", ["idle", "absolute"])
async def test_every_lifecycle_wait_rechecks_fresh_time_and_rolls_back_assets(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, path: str, stage: str, expiry_kind: str
) -> None:
    """全 lookup/delete/flush 待機の期限同時刻を、新しい now で拒否する。"""
    session = await prepare(monkeypatch, operation, path)
    if expiry_kind == "idle":
        session.auth_session.absolute_expires_at = EXPIRY + timedelta(hours=1)
    else:
        session.auth_session.idle_expires_at = EXPIRY + timedelta(hours=1)
    before = session.frozen_values()

    def waited(point: str) -> None:
        """元期限は動かさず、特定の待機が終わる瞬間だけ現在時刻を進める。"""
        if point == stage:
            Clock.current = EXPIRY

    session.on_step = waited
    with pytest.raises(UnauthorizedSessionError):
        await session.operate()
    assert stage in session.timeline
    assert session.frozen_values() == before
    assert session.commits == 0 and session.rollbacks == 1
    if not stage.startswith(("delete-", "flush:")):
        assert session.failure_values == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_final_time_just_before_expiry_is_accepted(
    monkeypatch: pytest.MonkeyPatch, operation: Operation
) -> None:
    """期限条件を拡張せず、最後の判定点でまだ有効な原会話は受理する。"""
    session = await prepare(monkeypatch, operation, "new")
    Clock.current = EXPIRY - timedelta(microseconds=1)
    await session.operate()
    assert session.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("expired", [False, True])
async def test_missing_manifest_refuses_without_mutation_and_rechecks_current_session(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, expired: bool
) -> None:
    """凍結内容の欠落は静的な領域拒否とし、待機中の失効を後着情報より優先する。"""
    session = await prepare(monkeypatch, operation, "new")
    session.missing.add(RuntimeManifest)
    before = session.frozen_values()

    def waited(point: str) -> None:
        """欠落応答が返った時点で失効しても、古い資格で対象の状態を返さない。"""
        if expired and point == "select-RuntimeManifest:1":
            Clock.current = EXPIRY

    session.on_step = waited
    expected: type[Exception] = (
        UnauthorizedSessionError
        if expired
        else {
            "deprecate": SkillVersionTransitionError,
            "delete": SkillVersionDeleteBlockedError,
            "enable": SkillVersionEnablementConflictError,
            "disable": SkillVersionEnablementConflictError,
        }[operation]
    )
    with pytest.raises(expected):
        await session.operate()
    assert session.frozen_values() == before and session.failure_values == before
    assert session.mutations == [] and session.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["enable", "disable"])
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stage", ["get-SkillVersion:1", "binding:1", "flush:1"])
@pytest.mark.parametrize("denial", ["archived", "foreign"])
async def test_locked_project_is_checked_again_after_lifecycle_waits(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, path: str, stage: str, denial: str
) -> None:
    """現在 Project の防御的再検査を示し、実 lock writer の並行更新とは主張しない。"""
    session = await prepare(monkeypatch, operation, path)
    before = session.frozen_values()

    def changed(point: str) -> None:
        """資格だけを外部変更し、今回の資産 rollback では復活させない。"""
        if point == stage:
            if denial == "archived":
                session.project.status = "ARCHIVED"
            else:
                session.project.organization_id = uuid4()

    session.on_step = changed
    with pytest.raises(ProjectArchivedError if denial == "archived" else ProjectNotFoundError):
        await session.operate()
    assert stage in session.timeline
    assert session.frozen_values() == before and session.commits == 0
    if denial == "archived":
        assert session.project.status == "ARCHIVED"
    else:
        assert session.project.organization_id != session.user.organization_id


@pytest.mark.asyncio
async def test_disabled_enablement_cannot_be_reactivated_or_change_original_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """現在 ADMIN の再認証を、新しい再有効化プロトコルと混同しない。"""
    session = await prepare(monkeypatch, "disable", "replay")
    before = session.frozen_values()
    session.operation = "enable"
    with pytest.raises(SkillVersionEnablementConflictError):
        await session.operate()
    assert session.frozen_values() == before
    assert session.binding.disabled_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_field", ["project_id", "skill_version_id"])
async def test_disable_never_uses_another_project_or_version_binding(
    monkeypatch: pytest.MonkeyPatch, foreign_field: str
) -> None:
    """別の組合せの binding が存在しても、要求した精確関係の不存在は 404 のままにする。"""
    session = await prepare(monkeypatch, "disable", "new")
    setattr(session.binding, foreign_field, uuid4())
    before = session.frozen_values()
    with pytest.raises(SkillVersionEnablementNotFoundError):
        await session.operate()
    assert session.frozen_values() == before and session.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_model", REFERENCE_MODELS)
@pytest.mark.parametrize("version_status", ["DRAFT", "DEPRECATED"])
async def test_delete_keeps_each_existing_audit_reference_before_deleting_anything(
    monkeypatch: pytest.MonkeyPatch, reference_model: type, version_status: str
) -> None:
    """六種類の実行/構成参照は状態で除外せず、現在 ADMIN でも削除できない。"""
    session = await prepare(monkeypatch, "delete", "new")
    session.version.status = version_status
    session.references[reference_model].add(session.version.id)
    before = session.frozen_values()
    with pytest.raises(SkillVersionDeleteBlockedError):
        await session.operate()
    assert session.frozen_values() == before and session.mutations == []


@pytest.mark.asyncio
async def test_delete_scope_removes_only_bindings_of_original_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同 Project の別版は残し、原版の複数 Project の設定だけを同じ transaction で除く。"""
    session = await prepare(monkeypatch, "delete", "new")
    unrelated = ProjectSkillVersion(
        id=uuid4(),
        project_id=session.project.id,
        skill_version_id=uuid4(),
        enabled_by=session.user.id,
        enabled_at=NOW,
        disabled_at=None,
    )
    another_project = ProjectSkillVersion(
        id=uuid4(),
        project_id=uuid4(),
        skill_version_id=session.version.id,
        enabled_by=session.user.id,
        enabled_at=NOW,
        disabled_at=None,
    )
    session.bindings.extend([unrelated, another_project])
    untouched = session.binding_values(unrelated)
    await session.operate()
    assert session.bindings == [unrelated]
    assert session.binding_values(unrelated) == untouched
    assert session.skill in session.rows


SQL_STAGES = [(operation, "flush:1") for operation in OPERATIONS] + [
    ("delete", stage)
    for stage in (
        "references-RunSkillSnapshot:1",
        "delete-bindings:1",
        "delete-RuntimeManifest:1",
        "delete-SkillVersion:1",
    )
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,stage", SQL_STAGES)
@pytest.mark.parametrize("expired", [False, True])
async def test_sql_failures_restore_assets_and_never_lazy_load_expired_credential(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, stage: str, expired: bool
) -> None:
    """SQL 失敗後の資格分類は private snapshot を使い、削除済みの局部資産も戻す。"""
    session = await prepare(monkeypatch, operation, "new")
    before = session.frozen_values()
    error = OperationalError("Synthetic lifecycle SQL failure", {}, RuntimeError("not connected"))
    make_transient_to_detached(session.user)
    make_transient_to_detached(session.auth_session)
    make_transient_to_detached(session.project)
    with Session() as database:
        database.add_all([session.user, session.auth_session, session.project])
        attempted: list[str] = []

        def reject_sql(state: ORMExecuteState) -> None:
            """元の failed ORM から新しい SELECT を発行したら即時失敗させる。"""
            attempted.append(str(state.statement))
            raise AssertionError("Failure classification must not load ORM")

        def fail(point: str) -> None:
            """指定 SQL 待機の後に元 ORM を失効させ、同じ原例外を送る。"""
            if point == stage:
                database.expire_all()
                if expired:
                    Clock.current = EXPIRY
                raise error

        session.on_step = fail
        event.listen(database, "do_orm_execute", reject_sql)
        try:
            with pytest.raises(UnauthorizedSessionError if expired else OperationalError) as result:
                await session.operate()
            if not expired:
                assert result.value is error
            assert attempted == []
            assert inspect(session.user).expired_attributes
            assert session.frozen_values() == before
            assert session.rollbacks == 1 and session.commits == 0
        finally:
            event.remove(database, "do_orm_execute", reject_sql)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,stage", SQL_STAGES)
async def test_cancellation_never_retries_and_restores_its_uncommitted_assets(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, stage: str
) -> None:
    """commit 前の取消は原例外を保持し、補償 transaction や自動 retry を始めない。"""
    session = await prepare(monkeypatch, operation, "new")
    before = session.frozen_values()
    cancellation = asyncio.CancelledError()

    def cancel(point: str) -> None:
        """今回の合成 cancellation は指定の SQL 待機で発生する。"""
        if point == stage:
            raise cancellation

    session.on_step = cancel
    with pytest.raises(asyncio.CancelledError) as result:
        await session.operate()
    assert result.value is cancellation
    assert session.frozen_values() == before
    assert session.transactions == session.rollbacks == 1 and session.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("outcome", ["committed", "not-committed"])
@pytest.mark.parametrize("error_kind", ["connection", "sqlalchemy", "operational"])
async def test_unknown_commit_preserves_original_exception_and_never_retries(
    monkeypatch: pytest.MonkeyPatch, operation: Operation, outcome: str, error_kind: str
) -> None:
    """commit で期限を越えても SQL 応答喪失を新しい認証エラーに変換しない。"""
    session = await prepare(monkeypatch, operation, "new")
    before = session.frozen_values()
    if error_kind == "operational":
        session.commit_error = OperationalError(
            "Synthetic lifecycle commit unknown", {}, RuntimeError("not connected")
        )
    elif error_kind == "sqlalchemy":
        session.commit_error = SQLAlchemyError("Synthetic lifecycle commit unknown")
    session.commit_outcome = outcome

    def commit_wait(point: str) -> None:
        """最後の業務判定と物理 commit の時刻は同一ではないことを再現する。"""
        if point == "commit:1":
            Clock.current = EXPIRY

    session.on_step = commit_wait
    with pytest.raises(type(session.commit_error)) as result:
        await session.operate()
    assert result.value is session.commit_error
    assert session.transactions == 1
    assert session.commits == (1 if outcome == "committed" else 0)
    assert (session.frozen_values() == before) is (outcome == "not-committed")


@pytest.mark.asyncio
async def test_current_task_binding_uses_version_then_binding_share_without_rewriting_assets() -> (
    None
):
    """新規作成用の guard は二つの精確 SHARE を使い、解析や Manifest 更新を行わない。"""
    session = LifecycleSession("disable")
    before = session.frozen_values()
    await session.check_current_binding()
    assert session.timeline == ["task-version:1", "task-binding:1"]
    assert "FOR SHARE OF skill_versions" in session.queries[0]
    assert "FOR SHARE" in session.queries[1]
    assert session.frozen_values() == before and session.mutations == []
    assert session.loads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing-version",
        "deprecated",
        "draft",
        "missing-source",
        "foreign-source",
        "foreign-skill",
        "foreign-organization",
        "missing-project",
        "foreign-project",
        "missing-binding",
        "disabled",
        "binding-project",
        "binding-version",
    ],
)
async def test_current_task_binding_rejects_stale_or_foreign_saved_relation(case: str) -> None:
    """過去に解析した版があっても、現在の保存版/帰属/精確な有効化が無ければ拒否する。"""
    session = LifecycleSession("disable")
    if case == "missing-version":
        session.missing.add(SkillVersion)
    elif case in {"deprecated", "draft"}:
        session.version.status = case.upper()
    elif case == "missing-source":
        session.missing.add(SkillSource)
    elif case == "foreign-source":
        session.source.organization_id = uuid4()
    elif case == "foreign-skill":
        session.skill.organization_id = uuid4()
    elif case == "foreign-organization":
        session.access = replace(
            session.access, actor=replace(session.access.actor, organization_id=uuid4())
        )
    elif case == "missing-project":
        session.project_present = False
    elif case == "foreign-project":
        session.project.organization_id = uuid4()
    elif case == "missing-binding":
        session.bindings.clear()
    elif case == "disabled":
        session.binding.disabled_at = NOW
    elif case == "binding-project":
        session.binding.project_id = uuid4()
    else:
        session.binding.skill_version_id = uuid4()
    before = session.frozen_values()
    with pytest.raises(PublishedTaskNotFoundError):
        await session.check_current_binding()
    assert session.frozen_values() == before and session.mutations == []
    assert session.timeline[0] == "task-version:1"
    if case in {"missing-binding", "disabled", "binding-project", "binding-version"}:
        assert session.timeline == ["task-version:1", "task-binding:1"]
    else:
        assert session.timeline == ["task-version:1"]
