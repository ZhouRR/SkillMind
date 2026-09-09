"""原会話・参照・commit の各境界を、実用例と SQL fake で回帰する。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.db.models import (
    AuthSession,
    Organization,
    Project,
    ProjectDocument,
    ProjectMember,
    Run,
    User,
)
from projectmind.documents.domain import (
    DocumentInUseError,
    DocumentNotFoundError,
    DocumentReferencesUnavailableError,
)
from projectmind.documents.references import (
    choice_document_ids,
    run_document_ids,
)
from projectmind.documents.snapshot import (
    DOCUMENT_PROVIDER,
    DOCUMENT_READ_CAPABILITY,
    freeze_document_snapshot,
    parse_document_selection,
)
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from projectmind.runs.domain import RunStatus
from projectmind.schedules.domain import (
    ScheduleConflictError,
    ScheduleDefinition,
    ScheduleInvalidError,
    ScheduleKind,
)
from tests.documents.deletion_harness import DeletionDatabase
from tests.documents.fakes import document_content, stored_document
from tests.runs.creation_fakes import creation_command, creation_intent, stored_creation
from tests.schedules.fakes import occurrence_row


def referenced_run(
    db: DeletionDatabase, *, all_documents: bool = False, legacy: bool = False
) -> Run:
    """本番 codec と request hash で原意図/初回メンバーを生成する。"""

    token = "project-documents:all" if all_documents else f"document:{db.document.id}"
    intent = replace(creation_intent(sources={"docs": token}), project_id=db.project.id)
    metadata = stored_document(db.project.id, document_content())
    snapshot = freeze_document_snapshot(
        project_id=db.project.id,
        requirement_key="docs",
        selection=parse_document_selection(token),
        documents=[metadata],
    )
    command = replace(
        creation_command(intent, legacy=legacy),
        selected_sources_json={
            "docs": {
                "provider": DOCUMENT_PROVIDER,
                "capability": DOCUMENT_READ_CAPABILITY,
                "resource_kind": "document",
                "candidate_key": token,
                "document_snapshot": snapshot.to_json(),
            }
        },
    )
    return stored_creation(command)


async def test_unreferenced_delete_commits_then_deletes_only_original_blob() -> None:
    """原会話と Project/文書锁を保持して metadata を commit し、その後だけ blob に進む。"""

    db = DeletionDatabase()
    await db.remove_document()
    assert not db.documents and db.commits == 1
    assert db.lock_events == [
        Organization,
        User,
        AuthSession,
        Project,
        ProjectMember,
        ProjectDocument,
    ]
    db.storage.delete.assert_awaited_once_with(db.document.storage_key)


@pytest.mark.parametrize("status", [item.value for item in RunStatus])
@pytest.mark.parametrize("all_documents,legacy", [(False, False), (True, False), (False, True)])
async def test_every_run_state_retains_original_frozen_document(
    status: str, all_documents: bool, legacy: bool
) -> None:
    """終態や旧 request 形式でも原メンバーを保護し、履歴を書き換えない。"""

    db = DeletionDatabase()
    run = referenced_run(db, all_documents=all_documents, legacy=legacy)
    run.status = status
    before = deepcopy(run.selected_sources_json)
    db.runs.append(run)
    with pytest.raises(DocumentInUseError):
        await db.remove_document()
    assert db.documents == [db.document] and db.rollbacks == 1
    assert run.selected_sources_json == before
    db.storage.delete.assert_not_called()


@pytest.mark.parametrize("status", ["ACTIVE", "PAUSED", "ERROR", "COMPLETED", "ARCHIVED"])
async def test_current_schedule_in_all_states_pins_explicit_document(status: str) -> None:
    """暂停や未発火も明示参照の解除条件にしない。"""

    db = DeletionDatabase()
    db.schedule.status = status
    db.schedule.sources_json = {"docs": f"document:{db.document.id}"}
    with pytest.raises(DocumentInUseError):
        await db.remove_document()
    assert db.documents


@pytest.mark.parametrize("status", ["PENDING", "SETTLED"])
async def test_retained_occurrence_old_configuration_pins_document_after_edit(status: str) -> None:
    """無 Run の終結監査も原選択を保持し、親配置の変更で消さない。"""

    db = DeletionDatabase()
    db.schedule.sources_json = {"docs": f"document:{db.document.id}"}
    occurrence = occurrence_row(db.schedule)
    occurrence.status = status
    if status == "SETTLED":
        occurrence.outcome = "FAILED_PRECONDITION"
        occurrence.settled_at = datetime.now(UTC)
    db.occurrences.append(occurrence)
    db.schedule.sources_json = {}
    with pytest.raises(DocumentInUseError):
        await db.remove_document()


async def test_dynamic_all_does_not_pin_current_documents_but_run_all_does_not_grow() -> None:
    """未展開の全集は保存ルールであり、Run 初回固定後の新 ID を占有しない。"""

    db = DeletionDatabase()
    db.schedule.sources_json = {"docs": "project-documents:all"}
    db.occurrences.append(occurrence_row(db.schedule))
    run = referenced_run(db, all_documents=True)
    original_id = db.document.id
    db.document.id = uuid4()
    db.document.storage_key = f"projects/{db.project.id}/{db.document.id}"
    db.runs.append(run)
    assert original_id in run_document_ids(run) and db.document.id not in run_document_ids(run)
    await db.remove_document()


async def test_valid_run_without_selected_optional_documents_does_not_pin_assets() -> None:
    """許可 capability の宣言や任意 input 内 UUID を文書参照と解釈しない。"""

    db = DeletionDatabase()
    intent = replace(
        creation_intent(), project_id=db.project.id, input_json={"text": str(db.document.id)}
    )
    run = stored_creation(creation_command(intent))
    run.permission_snapshot_json["allowed_capabilities"] = [DOCUMENT_READ_CAPABILITY]
    db.runs.append(run)
    await db.remove_document()


@pytest.mark.parametrize(
    "corrupt", ["hash", "missing", "project", "slot", "id", "candidate", "intent", "unknown"]
)
async def test_corrupt_frozen_reference_is_unverifiable_not_empty(corrupt: str) -> None:
    """欠損・別 Project・異なる原意図・旧不明 source は無参照に投影しない。"""

    db = DeletionDatabase()
    run = referenced_run(db)
    source = run.selected_sources_json["docs"]
    if corrupt == "hash":
        source["document_snapshot"]["checksum"] = "sha256:" + "0" * 64
    elif corrupt == "missing":
        del source["document_snapshot"]
    elif corrupt == "project":
        source["document_snapshot"]["project_id"] = str(uuid4())
    elif corrupt == "slot":
        source["document_snapshot"]["requirement_key"] = "other"
    elif corrupt == "id":
        source["document_snapshot"]["documents"][0]["document_id"] = str(uuid4())
    elif corrupt == "candidate":
        source["candidate_key"] = "project-documents:all"
    elif corrupt == "intent":
        run.request_hash = "0" * 64
    else:
        run.selected_sources_json["extra"] = {"provider": "unknown"}
    db.runs.append(run)
    with pytest.raises(DocumentReferencesUnavailableError):
        await db.remove_document()
    assert db.documents and db.rollbacks == 1
    db.storage.delete.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [None, [], {"docs": "project"}, {"docs": "redmine"}, {"docs": "document:bad"}, {"docs": 1}],
)
def test_unverifiable_choice_is_not_silently_empty(value: object) -> None:
    """意味を証明できない旧表記と壊れた外形を拒否する。"""

    with pytest.raises((ValueError, TypeError)):
        choice_document_ids(value)


@pytest.mark.parametrize("broken", ["checksum", "parent", "project", "actor", "indexed"])
async def test_occurrence_outer_join_retains_unverifiable_association(broken: str) -> None:
    """片側だけが原 Project の壊れた関連も JOIN で消さず拒否する。"""

    db = DeletionDatabase()
    row = occurrence_row(db.schedule)
    db.occurrences.append(row)
    if broken == "checksum":
        row.snapshot_checksum = "0" * 64
    elif broken == "parent":
        db.schedules.clear()
    elif broken == "project":
        db.schedule.project_id = uuid4()
    elif broken == "actor":
        db.schedule.created_by = uuid4()
    else:
        row.configuration_version += 1
    with pytest.raises(DocumentReferencesUnavailableError):
        await db.remove_document()


@pytest.mark.parametrize(
    "lock", [Organization, User, AuthSession, Project, ProjectMember, ProjectDocument]
)
async def test_session_expiry_after_each_lock_rolls_back_without_blob_io(lock: type) -> None:
    """各待機終了後の現在時刻を使い、入口の古い期限を再利用しない。"""

    db = DeletionDatabase()

    def expire(entity: type) -> None:
        """待機点だけに過去の期限を注入する。"""
        if entity is lock:
            db.auth_session.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    db.on_lock = expire
    with pytest.raises(UnauthorizedSessionError):
        await db.remove_document()
    assert db.documents and db.rollbacks == 1
    db.storage.delete.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["revoked", "csrf", "member", "archived", "missing", "wrong_project", "final_expiry"]
)
async def test_current_authorization_and_final_flush_protect_deletion(failure: str) -> None:
    """原会話/Project 拒否と、削除 staging 後の期限失効を明確に区別する。"""

    db = DeletionDatabase()
    error: type[Exception]
    if failure == "revoked":
        db.auth_session.revoked_at = datetime.now(UTC)
        error = UnauthorizedSessionError
    elif failure == "csrf":
        db.access = replace(db.access, csrf_token="wrong")
        error = CsrfRejectedError
    elif failure == "member":
        db.member = None
        error = ProjectNotFoundError
    elif failure == "archived":
        db.project.status = "ARCHIVED"
        error = ProjectArchivedError
    elif failure == "missing":
        db.documents.clear()
        error = DocumentNotFoundError
    elif failure == "wrong_project":
        db.document.project_id = uuid4()
        error = DocumentNotFoundError
    else:

        def expire() -> None:
            """最後の flush 後に失効させる。"""
            db.auth_session.absolute_expires_at = datetime.now(UTC) - timedelta(seconds=1)

        db.on_flush = expire
        error = UnauthorizedSessionError
    before = list(db.documents)
    with pytest.raises(error):
        await db.remove_document()
    assert db.documents == before and db.rollbacks == 1
    db.storage.delete.assert_not_called()


@pytest.mark.parametrize("persists", [False, True])
async def test_commit_unknown_never_deletes_blob_or_retries(persists: bool) -> None:
    """実際の持続有無にかかわらず、commit 応答未知では blob を触らない。"""

    db = DeletionDatabase()
    db.commit_error = True
    db.commit_persists = persists
    with pytest.raises(ConnectionError):
        await db.remove_document()
    assert bool(db.documents) is not persists
    assert db.transactions == 1
    db.storage.delete.assert_not_called()


async def test_commit_barrier_delays_storage_and_cancel_keeps_original_blob() -> None:
    """commit 待機中の取消は rollback 合成を検証し、現実の commit 不成立とは外推しない。"""

    db = DeletionDatabase()
    db.commit_release = asyncio.Event()
    task = asyncio.create_task(db.remove_document())
    await asyncio.wait_for(db.commit_entered.wait(), 2)
    db.storage.delete.assert_not_called()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.documents and db.rollbacks == 1


@pytest.mark.parametrize("operation", ["create", "edit"])
async def test_schedule_rechecks_original_document_after_outside_validation(operation: str) -> None:
    """锁外で存在した原 ID が保存門禁取得前に消えた場合、悬空 sources を保存しない。"""

    db = DeletionDatabase()
    original = f"document:{db.document.id}"
    sources = {"docs": original}
    payload = {"nested": ["original"]}

    async def validated(**arguments: object) -> None:
        """caller 可変 dict と実 metadata の競争を、既存の検証待機点で注入する。"""
        assert not db.in_transaction
        sources.clear()
        payload["nested"].append("changed")
        db.documents.clear()

    db.runs_service.validate_task_sources.side_effect = validated
    definition = ScheduleDefinition(ScheduleKind.CRON, "UTC", "*/5 * * * *", None, None, 5)
    before = deepcopy(db.schedule.sources_json)
    with pytest.raises(ScheduleInvalidError, match="no longer available"):
        if operation == "create":
            await db.service.create_schedule(
                project_id=db.project.id,
                access=db.access,
                name="New",
                definition=definition,
                skill_version_id=db.schedule.skill_version_id,
                task_key=db.schedule.task_key,
                input_json=payload,
                sources=sources,
            )
        else:
            await db.service.update_schedule(
                project_id=db.project.id,
                schedule_id=db.schedule.id,
                access=db.access,
                name="Edited",
                definition=definition,
                input_json=payload,
                sources=sources,
                expected_row_version=db.schedule.row_version,
            )
    assert db.schedule.sources_json == before and len(db.schedules) == 1
    assert db.rollbacks == 1


async def test_admin_still_requires_original_session_but_not_membership() -> None:
    """ADMIN bypass は所属だけに限り、原 login の角色と資格は確認する。"""

    db = DeletionDatabase()
    db.user.system_role = "ADMIN"
    db.auth_session.system_role_at_login = "ADMIN"
    db.member = None
    await db.remove_document()
    assert ProjectMember not in db.lock_events
    assert not db.documents


async def test_another_live_session_of_same_actor_does_not_replace_revoked_original() -> None:
    """同じ user の新会話が有効でも、取り消された原 cookie を選び直さない。"""

    db = DeletionDatabase()
    from projectmind.auth.domain import generate_session_credentials

    credentials = generate_session_credentials()
    other = AuthSession(
        id=uuid4(),
        user_id=db.user.id,
        token_hash=credentials.session_token_hash,
        csrf_token_hash=credentials.csrf_token_hash,
        credential_version=2,
        system_role_at_login="USER",
        revoked_at=None,
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
        absolute_expires_at=datetime.now(UTC) + timedelta(hours=8),
    )
    db.auth_sessions.insert(0, other)
    db.auth_session.revoked_at = datetime.now(UTC)
    with pytest.raises(UnauthorizedSessionError):
        await db.remove_document()
    assert db.documents and db.rollbacks == 1
    db.storage.delete.assert_not_called()


async def test_metadata_get_uses_no_business_transaction_or_storage() -> None:
    """正確な原 ID の読取を blob GET/DELETE や新規 write に変換しない。"""

    db = DeletionDatabase()
    value = await db.document_service.get_document(
        project_id=db.project.id, document_id=db.document.id
    )
    assert value.document_id == db.document.id and db.documents
    assert db.transactions == 0 and not db.lock_events and not db.storage.mock_calls


async def test_unknown_history_in_other_project_does_not_block_this_project() -> None:
    """他 Project の未知な記録を現在の門禁へ混ぜず、範囲付き WHERE を評価する。"""

    db = DeletionDatabase()
    unrelated = stored_creation(creation_command(creation_intent()))
    unrelated.selected_sources_json = {"legacy": "project"}
    db.runs.append(unrelated)
    await db.remove_document()


async def test_locked_schedule_version_conflict_precedes_disappeared_document() -> None:
    """保存待機中の tick/編集による原版衝突を、文書失効 422 に読み替えない。"""

    db = DeletionDatabase()
    from projectmind.db.models import TaskSchedule

    def change(entity: type) -> None:
        """同じ锁待機点で版変化と原文書消失を注入する。"""
        if entity is TaskSchedule:
            db.schedule.row_version += 1
            db.documents.clear()

    db.on_lock = change
    definition = ScheduleDefinition(ScheduleKind.CRON, "UTC", "*/5 * * * *", None, None, 5)
    with pytest.raises(ScheduleConflictError):
        await db.service.update_schedule(
            project_id=db.project.id,
            schedule_id=db.schedule.id,
            access=db.access,
            name="Edited",
            definition=definition,
            input_json={},
            sources={"docs": f"document:{db.document.id}"},
            expected_row_version=db.schedule.row_version,
        )
    assert db.schedule.sources_json == {} and db.rollbacks == 1
