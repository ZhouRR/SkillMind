"""三組合操作の実資格・精確版・合成 rollback を検証する。実 DB 競争は扱わない。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import ORMExecuteState, Session, make_transient_to_detached

from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.compositions.domain import ModuleNotFoundError, ModuleSkillInvalidError
from skillmind.db.models import (
    ProjectComposition,
    Skill,
    SkillComposition,
    SkillSource,
    SkillVersion,
)
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.users.domain import UserAdministrationDeniedError
from tests.compositions.composition_authorization_harness import (
    OPERATIONS,
    CompositionSession,
    Operation,
    columns,
)
from tests.skills.test_skill_publication_authorization import EXPIRY, NOW, Clock


def prepare(monkeypatch: pytest.MonkeyPatch, operation: Operation) -> CompositionSession:
    """原会話の時刻だけを制御し、本番 repository/authorizer は差し替えない。"""
    Clock.current = NOW
    monkeypatch.setattr("skillmind.compositions.service.datetime", Clock)
    session = CompositionSession(operation)
    session.auth_session.idle_expires_at = EXPIRY
    session.auth_session.absolute_expires_at = EXPIRY
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_current_admin_without_membership_uses_exact_locks_and_original_display_order(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
) -> None:
    """ADMIN でも原資格を固定し、宣言順と共有版 lock 順を分けて保持する。"""
    session = prepare(monkeypatch, operation)
    original = session.frozen_values()
    audit = [columns(row) for row in session.enablements]
    result = await session.operate()
    assert session.timeline[:4] == [
        "organization:1",
        "select-User:1",
        "select-AuthSession:1",
        "project:1",
    ]
    assert session.timeline[-2:] == ["flush:2", "commit:1"]
    assert session.commits == 1 and session.rollbacks == 0
    assert session.frozen_values()["manifest"] == original["manifest"]
    assert session.frozen_values()["versions"] == original["versions"]
    assert session.frozen_values()["source"] == original["source"]
    assert not any("project_members" in query for query in session.queries)
    if operation == "delete":
        assert result is None and session.compositions == [] and session.enablements == []
        assert session.locked_versions == []
        assert session.mutations == ["delete-ProjectComposition", "delete-SkillComposition"]
    else:
        assert result is not None
        assert result.name == "Updated module" and result.description == "Updated description"
        assert [item.skill_version_id for item in result.skills] == list(
            dict.fromkeys(session.version_ids)
        )
        assert [item.sort_order for item in result.skills] == [0, 1, 2]
        assert session.locked_versions == sorted(set(session.version_ids))
        assert [
            point.split(":")[0]
            for point in session.timeline
            if point.startswith(("version:", "binding:"))
        ] == ["version", "binding"] * 3
        if operation == "create":
            assert session.enablements[0].enabled_by == session.user.id
        else:
            assert [columns(row) for row in session.enablements] == audit
    if operation != "create":
        assert session.timeline.index("composition:1") < session.timeline.index("enablements:1")
        assert any("FOR UPDATE OF skill_compositions" in query for query in session.queries)
    if operation != "delete":
        assert sum("FOR SHARE OF skill_versions" in query for query in session.queries) == 3
        assert (
            sum(
                "FOR SHARE" in query and "project_skill_versions" in query
                for query in session.queries
            )
            == 3
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "denial",
    [
        "revoked",
        "disabled",
        "role-changed",
        "user",
        "csrf",
        "missing-session",
        "other-token",
        "foreign-user",
        "missing-user",
        "missing-org",
    ],
)
async def test_original_credential_and_admin_are_required_before_project_or_module_lookup(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    denial: str,
) -> None:
    """入口 actor が ADMIN でも現在の原会話と User を用い、USER の参加で管理を開放しない。"""
    session = prepare(monkeypatch, operation)
    expected: type[Exception] = UnauthorizedSessionError
    if denial == "revoked":
        session.auth_session.revoked_at = NOW
    elif denial == "disabled":
        session.user.status = "DISABLED"
    elif denial == "role-changed":
        session.user.system_role = "USER"
    elif denial == "user":
        session.user.system_role = session.auth_session.system_role_at_login = "USER"
        expected = UserAdministrationDeniedError
    elif denial == "csrf":
        session.access = replace(session.access, csrf_token="")
        expected = CsrfRejectedError
    elif denial == "missing-session":
        session.auth_sessions.clear()
    elif denial == "other-token":
        session.auth_session.token_hash = "sha256:" + "a" * 64
    elif denial == "foreign-user":
        session.user.organization_id = uuid4()
    elif denial == "missing-user":
        session.users.clear()
    else:
        session.authority.organization = None
    before = session.frozen_values()
    with pytest.raises(expected):
        await session.operate()
    assert session.frozen_values() == before and session.mutations == []
    assert "project:1" not in session.timeline and session.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("denial", ["missing", "foreign", "archived"])
async def test_locked_exact_project_is_required_for_every_write(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    denial: str,
) -> None:
    """ADMIN の membership 免除と、他組織/削除/アーカイブ Project の許可を混同しない。"""
    session = prepare(monkeypatch, operation)
    if denial == "missing":
        session.project_present = False
    elif denial == "foreign":
        session.project.organization_id = uuid4()
    else:
        session.project.status = "ARCHIVED"
    before = session.frozen_values()
    with pytest.raises(ProjectArchivedError if denial == "archived" else ProjectNotFoundError):
        await session.operate()
    assert session.frozen_values() == before and session.mutations == []
    assert session.locked_versions == [] and session.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    "case",
    [
        "missing-version",
        "draft",
        "deprecated",
        "foreign-skill",
        "foreign-source",
        "missing-skill",
        "missing-source",
        "version-skill",
        "version-source",
        "missing-binding",
        "disabled",
        "binding-project",
        "binding-version",
        "late-invalid",
    ],
)
async def test_selected_versions_require_current_exact_organization_and_project_binding(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    case: str,
) -> None:
    """同じ静的拒否に畳み、別版が通過した後の拒否でも集合を部分保存しない。"""
    session = prepare(monkeypatch, operation)
    if case == "missing-version":
        session.missing.add(SkillVersion)
    elif case in {"draft", "deprecated"}:
        session.version.status = case.upper()
    elif case == "foreign-skill":
        session.skill.organization_id = uuid4()
    elif case == "foreign-source":
        session.source.organization_id = uuid4()
    elif case == "missing-skill":
        session.missing.add(Skill)
    elif case == "missing-source":
        session.missing.add(SkillSource)
    elif case == "version-skill":
        session.version.skill_id = uuid4()
    elif case == "version-source":
        session.version.skill_source_id = uuid4()
    elif case == "missing-binding":
        session.bindings.clear()
    elif case == "disabled":
        session.bindings[0].disabled_at = NOW
    elif case == "binding-project":
        session.bindings[0].project_id = uuid4()
    elif case == "binding-version":
        session.bindings[0].skill_version_id = uuid4()
    else:
        session.bindings[-1].disabled_at = NOW
    before = session.frozen_values()
    with pytest.raises(
        ModuleSkillInvalidError, match=r"^Skill versions are not available for this project$"
    ):
        await session.operate()
    assert session.frozen_values() == before and session.mutations == []
    assert session.commits == 0
    if case == "late-invalid":
        assert session.locked_versions == sorted(set(session.version_ids))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize(
    "case", ["missing", "foreign", "presentation", "relation-project", "relation-parent"]
)
async def test_existing_module_requires_exact_parent_and_current_project_relationship(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    case: str,
) -> None:
    """共有父行の存在だけでは更新/削除権を与えず、他 Project の関連を代用しない。"""
    session = prepare(monkeypatch, operation)
    assert session.composition is not None
    if case == "missing":
        session.missing.add(SkillComposition)
    elif case == "foreign":
        session.composition.organization_id = uuid4()
    elif case == "presentation":
        session.composition.presentation = "role"
    elif case == "relation-project":
        session.enablements[0].project_id = uuid4()
    else:
        session.enablements[0].composition_id = uuid4()
    before = session.frozen_values()
    with pytest.raises(ModuleNotFoundError):
        await session.operate()
    assert session.frozen_values() == before and session.mutations == []


WAIT_CASES = [
    (operation, stage)
    for operation in OPERATIONS
    for stage in (
        "organization:1",
        "select-User:1",
        "select-AuthSession:1",
        "project:1",
        *(("composition:1", "enablements:1") if operation != "create" else ()),
        *(
            (
                "version:1",
                "binding:1",
                "version:2",
                "binding:2",
                "version:3",
                "binding:3",
                "projection:1",
            )
            if operation != "delete"
            else ("delete-ProjectComposition:1", "delete-SkillComposition:1")
        ),
        "flush:1",
        "flush:2",
    )
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,stage", WAIT_CASES)
@pytest.mark.parametrize("expiry_kind", ["idle", "absolute"])
async def test_each_wait_uses_fresh_time_and_rolls_back_uncommitted_assets(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    stage: str,
    expiry_kind: str,
) -> None:
    """SQL/表示読取/削除/最終 flush の各完了時刻が原期限と等しい場合も拒否する。"""
    session = prepare(monkeypatch, operation)
    if expiry_kind == "idle":
        session.auth_session.absolute_expires_at = EXPIRY + timedelta(hours=1)
    else:
        session.auth_session.idle_expires_at = EXPIRY + timedelta(hours=1)
    before = session.frozen_values()

    def waited(point: str) -> None:
        """指定した一回の境界だけで時計を進め、実待機を発生させない。"""
        if point == stage:
            Clock.current = EXPIRY

    session.on_step = waited
    with pytest.raises(UnauthorizedSessionError):
        await session.operate()
    assert stage in session.timeline
    assert session.frozen_values() == before
    assert session.transactions == session.rollbacks == 1 and session.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_last_microsecond_before_expiry_can_commit(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
) -> None:
    """現行の等号境界を強めず、最後まで有効な会話は三操作を実行できる。"""
    session = prepare(monkeypatch, operation)
    Clock.current = EXPIRY - timedelta(microseconds=1)
    await session.operate()
    assert session.commits == 1


SQL_STAGES = [
    (operation, stage)
    for operation, stage in WAIT_CASES
    if not stage.startswith(("organization:", "select-"))
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,stage", SQL_STAGES)
@pytest.mark.parametrize("expired", [False, True])
async def test_failed_sql_classification_uses_private_snapshot_without_expired_orm_reads(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    stage: str,
    expired: bool,
) -> None:
    """実 Session の expire 属性から lazy SQL を試みたら失敗させ、資産だけを復元する。"""
    session = prepare(monkeypatch, operation)
    before = session.frozen_values()
    failure = OperationalError("Synthetic SQL failure", {}, RuntimeError("not connected"))
    for row in (session.user, session.auth_session, session.project):
        make_transient_to_detached(row)
    with Session() as database:
        database.add_all([session.user, session.auth_session, session.project])
        attempted: list[str] = []

        def reject_sql(state: ORMExecuteState) -> None:
            """失敗分類のために元 ORM を読むことを許可しない。"""
            attempted.append(str(state.statement))
            raise AssertionError("Failure classification must not load ORM")

        def fail(point: str) -> None:
            """SQL が失敗した直後に原資格と Project を expire させる。"""
            if point == stage:
                database.expire_all()
                if expired:
                    Clock.current = EXPIRY
                raise failure

        session.on_step = fail
        event.listen(database, "do_orm_execute", reject_sql)
        try:
            with pytest.raises(UnauthorizedSessionError if expired else OperationalError) as result:
                await session.operate()
            if not expired:
                assert result.value is failure
            assert attempted == [] and inspect(session.user).expired_attributes
            assert session.frozen_values() == before
            assert session.rollbacks == 1 and session.commits == 0
        finally:
            event.remove(database, "do_orm_execute", reject_sql)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,stage", SQL_STAGES)
async def test_cancel_before_commit_restores_assets_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    stage: str,
) -> None:
    """取消は原例外で返し、認証拒否や補償 retry に変換しない。"""
    session = prepare(monkeypatch, operation)
    before = session.frozen_values()
    cancellation = asyncio.CancelledError()

    def cancel(point: str) -> None:
        """一つの未提交 SQL 待機で取り消す。"""
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
async def test_commit_unknown_stays_original_even_after_expiry_and_orm_expiration(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    outcome: str,
    error_kind: str,
) -> None:
    """物理 commit 二結末を合成しても、原応答喪失を成功/新しい認証エラーにしない。"""
    session = prepare(monkeypatch, operation)
    before = session.frozen_values()
    if error_kind == "sqlalchemy":
        session.commit_error = SQLAlchemyError("Synthetic commit unknown")
    elif error_kind == "operational":
        session.commit_error = OperationalError("Synthetic commit unknown", {}, RuntimeError())
    session.commit_outcome = outcome
    for row in (session.user, session.auth_session, session.project):
        make_transient_to_detached(row)
    with Session() as database:
        database.add_all([session.user, session.auth_session, session.project])

        def commit_wait(point: str) -> None:
            """commit の待機だけで時刻と ORM を失効させる。"""
            if point == "commit:1":
                Clock.current = EXPIRY
                database.expire_all()

        session.on_step = commit_wait
        with pytest.raises(type(session.commit_error)) as result:
            await session.operate()
        assert result.value is session.commit_error
        assert session.transactions == 1
        assert session.commits == int(outcome == "committed")
        assert (session.frozen_values() == before) is (outcome == "not-committed")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_shared_module_keeps_other_project_relationship_and_original_enablement_audit(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
) -> None:
    """共有更新は一つの父行へ反映し、削除は現在 Project の関連だけを外す。"""
    session = prepare(monkeypatch, operation)
    other = ProjectComposition(
        id=uuid4(),
        project_id=uuid4(),
        composition_id=session.module_id,
        enabled_by=uuid4(),
        created_at=NOW - timedelta(days=1),
    )
    session.enablements.append(other)
    before = columns(other)
    original_parent = session.composition
    await session.operate()
    assert other in session.enablements and columns(other) == before
    assert session.compositions == [original_parent]
    if operation == "delete":
        assert session.enablements == [other]
        assert session.mutations == ["delete-ProjectComposition"]
        assert session.locked_versions == []
    else:
        assert original_parent is not None and original_parent.name == "Updated module"
        assert [row.skill_version_id for row in original_parent.items] == list(
            dict.fromkeys(session.version_ids)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unavailable", ["deprecated", "disabled", "missing-binding", "foreign-source"]
)
async def test_delete_does_not_require_versions_to_be_currently_available(
    monkeypatch: pytest.MonkeyPatch,
    unavailable: str,
) -> None:
    """無効版を使う設定を外すために、先に再有効化するよう要求しない。"""
    session = prepare(monkeypatch, "delete")
    if unavailable == "deprecated":
        session.version.status = "DEPRECATED"
    elif unavailable == "disabled":
        session.bindings[0].disabled_at = NOW
    elif unavailable == "missing-binding":
        session.bindings.clear()
    else:
        session.source.organization_id = uuid4()
    await session.operate()
    assert session.compositions == [] and session.locked_versions == []


@pytest.mark.asyncio
async def test_delete_second_request_is_not_found_not_a_fabricated_replay_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """削除の冪等な受付記録を補造せず、原公開の二回目 404 を維持する。"""
    session = prepare(monkeypatch, "delete")
    await session.operate()
    before = session.frozen_values()
    with pytest.raises(ModuleNotFoundError):
        await session.operate()
    assert session.frozen_values() == before and session.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_original_request_is_frozen_before_first_await(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
) -> None:
    """呼出側の配列/入口 actor が待機中に変わっても、元要求の tuple と資格を使う。"""
    session = prepare(monkeypatch, operation)
    supplied = list(session.version_ids)
    expected = list(dict.fromkeys(supplied))
    original_actor = session.access.actor

    def changed(point: str) -> None:
        """外部入力だけを変え、保存済みの実 User/Session は変更しない。"""
        if point == "organization:1":
            supplied[:] = [uuid4()]
            session.access = replace(session.access, actor=replace(original_actor, user_id=uuid4()))

    session.on_step = changed
    service = session.service()
    if operation == "create":
        result = await service.create_module(
            access=session.access,
            project_id=session.project.id,
            name="Original",
            description="",
            skill_version_ids=supplied,
        )
    else:
        result = await service.update_module(
            access=session.access,
            project_id=session.project.id,
            module_id=session.module_id,
            name="Original",
            description="",
            skill_version_ids=supplied,
        )
    assert [item.skill_version_id for item in result.skills] == expected
    assert session.enablements[0].enabled_by == original_actor.user_id
    assert session.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("position", [1, 2])
async def test_later_version_refusal_cannot_publish_partial_composition(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    position: int,
) -> None:
    """二番目/三番目の版が失効したとき、先に取得済みの SHARE を保存成功とみなさない。"""
    session = prepare(monkeypatch, operation)
    session.bindings[position].disabled_at = NOW
    before = session.frozen_values()
    with pytest.raises(ModuleSkillInvalidError):
        await session.operate()
    assert session.locked_versions == sorted(set(session.version_ids))[: position + 1]
    assert session.frozen_values() == before and session.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_relationship_recheck_missing_current_link_never_uses_other_shared_link(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
) -> None:
    """最初の join 後の関係一覧に原 Project が無ければ、共有先を代わりに削除しない。"""
    session = prepare(monkeypatch, operation)
    other = ProjectComposition(
        id=uuid4(),
        project_id=uuid4(),
        composition_id=session.module_id,
        enabled_by=uuid4(),
        created_at=NOW,
    )
    session.enablements.append(other)
    before = session.frozen_values()

    def remove_current(point: str) -> None:
        """二つの query の局部応答を変え、実 PostgreSQL lock 下の競争とは主張しない。"""
        if point == "composition:1":
            session.enablements[:] = [other]

    session.on_step = remove_current
    with pytest.raises(ModuleNotFoundError):
        await session.operate()
    assert "enablements:1" in session.timeline
    assert session.mutations == [] and session.commits == 0
    assert session.frozen_values() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("denial", ["revoked", "archived", "foreign-project"])
async def test_final_flush_uses_current_locked_identity_and_project(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    denial: str,
) -> None:
    """最後の SQL 待機後も読んだままの資格を無条件に信頼しない。"""
    session = prepare(monkeypatch, operation)
    before = session.frozen_values()

    def changed(point: str) -> None:
        """資格の変更は自分の資産 rollback では巻き戻さない。"""
        if point == "flush:2":
            if denial == "revoked":
                session.auth_session.revoked_at = NOW
            elif denial == "archived":
                session.project.status = "ARCHIVED"
            else:
                session.project.organization_id = uuid4()

    session.on_step = changed
    expected = {
        "revoked": UnauthorizedSessionError,
        "archived": ProjectArchivedError,
        "foreign-project": ProjectNotFoundError,
    }[denial]
    with pytest.raises(expected):
        await session.operate()
    assert session.frozen_values() == before and session.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("case", ["project", "target"])
@pytest.mark.parametrize("expired", [False, True])
async def test_delayed_domain_failure_does_not_expose_target_to_expired_request(
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    case: str,
    expired: bool,
) -> None:
    """対象の 404/422 より原資格の最新失効を優先し、資格が有効なら元領域拒否を保つ。"""
    session = prepare(monkeypatch, operation)
    if case == "project":
        session.project_present = False
        stage = "project:1"
        domain: type[Exception] = ProjectNotFoundError
    elif operation == "create":
        session.version.status = "DEPRECATED"
        stage = "version:1"
        domain = ModuleSkillInvalidError
    else:
        session.missing.add(SkillComposition)
        stage = "composition:1"
        domain = ModuleNotFoundError
    before = session.frozen_values()

    def waited(point: str) -> None:
        """対象 query が返る時刻だけを操作する。"""
        if expired and point == stage:
            Clock.current = EXPIRY

    session.on_step = waited
    with pytest.raises(UnauthorizedSessionError if expired else domain):
        await session.operate()
    assert session.frozen_values() == before and session.mutations == []
