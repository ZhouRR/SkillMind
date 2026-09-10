"""閉公開の原監査・認可・占用を実 service/repository と局部 SQL/transaction fake で検証する。

同組織 gate は合成であり、実 PostgreSQL、SDK 停止、遠端の精確清理を証明しない。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, tzinfo
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.db.models import (
    AuthSession,
    Organization,
    Project,
    ProjectDocumentUpload,
    ProjectMember,
    User,
)
from skillmind.documents.domain import (
    DocumentUploadAlreadyPublishedError,
    DocumentUploadClosedError,
    DocumentUploadClosureNotFoundError,
    DocumentUploadInvalidError,
    DocumentUploadNotFoundError,
    StoredDocumentUploadClosure,
)
from skillmind.documents.paths import document_storage_key
from skillmind.projects.domain import ProjectArchivedError
from skillmind.storage import StoredBlob, UploadLimits, UploadRejectedError
from tests.documents.test_document_upload_authorization import deny
from tests.documents.test_document_upload_intents import (
    _leave_pending,
    _login_again,
    _lookup,
    _restart,
    _usage,
)
from tests.documents.test_upload_intent_repository import _document
from tests.documents.upload_harness import UploadDatabase


async def _close(
    db: UploadDatabase,
    key: UUID,
) -> tuple[StoredDocumentUploadClosure, bool]:
    """人工の停止だけを実 service へ渡し、storage や目録 DELETE に置き換えない。"""

    return await db.document_service.close_upload(
        project_id=db.project.id,
        upload_key=key,
        access=db.access,
    )


async def _closure(db: UploadDatabase, key: UUID) -> StoredDocumentUploadClosure:
    """現在の同 actor と原 key で、独立した持続 closure だけを確認する。"""

    return await db.document_service.get_upload_closure(
        project_id=db.project.id,
        upload_key=key,
        access=db.access,
    )


def _columns(row: Any) -> dict[str, Any]:
    """ORM 内部状態ではなく、原保存 field 全体の非変更と rollback を比較する。"""

    return {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}


async def test_close_keeps_pending_receipt_target_charge_and_bytes_without_storage() -> None:
    """閉公開は別監査だけを保存し、元 v1 回执・原 namespace・占用を消さない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    intent = db.intents[0]
    before = _columns(intent)
    receipt = await _lookup(db, key)
    db.storage.namespace = None
    db.access = replace(db.access, request_id=uuid4())
    closed, created = await _close(db, key)
    assert created is True and len(db.closures) == 1
    assert set(asdict(closed)) == {
        "upload_key",
        "project_id",
        "document_id",
        "closed_at",
        "publication_state",
    }
    assert closed.upload_key == key and closed.project_id == db.project.id
    assert closed.document_id == intent.document_id and closed.publication_state == "CLOSED"
    assert closed.closed_at >= intent.created_at
    assert intent.publication_closed_at == closed.closed_at
    assert _columns(intent) == {**before, "publication_closed_at": closed.closed_at}
    audit = db.closures[0]
    assert (
        audit.upload_intent_id == intent.id
        and audit.actor_id == audit.requested_by == intent.actor_id
    )
    assert audit.request_id == db.access.request_id and audit.session_id == db.auth_session.id
    assert audit.upload_key == key and audit.document_id == intent.document_id
    assert audit.closed_at == closed.closed_at
    assert (await _lookup(db, key)) == receipt
    assert receipt.state == "PENDING" and receipt.document is None
    assert set(asdict(receipt)) == {"upload_key", "project_id", "state", "created_at", "document"}
    assert await _closure(db, key) == closed and await _usage(db) == 5
    assert not db.documents and not db.cleanups
    assert await db.blobs.get(intent.storage_key) == b"hello"
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()
    db.storage.get.assert_not_called()


async def test_replay_keeps_first_closure_audit_and_never_reopens_original_upload() -> None:
    """同鍵停止は同じ原監査を返し、別 request/session や現在時計で上書きしない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    closed, _ = await _close(db, key)
    original = _columns(db.closures[0])
    _login_again(db)
    assert await _close(db, key) == (closed, False)
    assert _columns(db.closures[0]) == original
    assert await _closure(db, key) == closed
    with pytest.raises(DocumentUploadClosedError):
        await db.upload(upload_key=key)
    assert await _usage(db) == 5 and len(db.intents) == len(db.closures) == 1
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


async def test_closed_path_can_be_reused_only_with_new_key_and_additional_quota() -> None:
    """path の開放を返金と混同せず、同名新 key は新 ID と独立した追加占用になる。"""

    db = UploadDatabase(limits=UploadLimits(10, 10, frozenset({"text/plain"})))
    key = await _leave_pending(db)
    closed, _ = await _close(db, key)
    replacement = await db.upload(upload_key=uuid4())
    assert replacement.document_id != closed.document_id
    assert len(db.intents) == 2 and len(db.documents) == 1 and await _usage(db) == 10
    assert await _closure(db, key) == closed
    assert (await _lookup(db, key)).state == "PENDING"
    with pytest.raises(UploadRejectedError) as caught:
        await db.upload(name="another.txt", upload_key=uuid4())
    assert caught.value.code == "project_quota_exceeded"
    assert len(db.intents) == 2 and db.storage.put.await_count == 2
    db.storage.delete.assert_not_awaited()


async def test_repository_rejects_stale_open_intent_even_if_service_guard_is_bypassed() -> None:
    """古い TX B が持つ open DTO でも、repository 自身が閉公開 marker/監査を再確認する。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    async with db.document_service._upload_transaction(db.access, db.project.id) as transaction:
        _, uploads, _ = transaction
        admitted = await uploads.find(
            organization_id=db.user.organization_id,
            project_id=db.project.id,
            actor_id=db.user.id,
            upload_key=key,
        )
    assert admitted is not None and admitted.publication_closed_at is None
    closed, _ = await _close(db, key)
    before = _columns(db.intents[0])
    with pytest.raises(DocumentUploadClosedError):
        async with db.document_service._upload_transaction(db.access, db.project.id) as transaction:
            _, uploads, _ = transaction
            await uploads.publish(admitted, _document(admitted))
    assert _columns(db.intents[0]) == before and not db.documents
    assert await _closure(db, key) == closed and await _usage(db) == 5
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("deleted", [False, True])
async def test_published_upload_cannot_be_closed_or_reinterpreted_as_delete(deleted: bool) -> None:
    """元公開回执がある場合は明示競合で、目録削除済みでも停止へ書換えない。"""

    db = UploadDatabase()
    published = await db.upload()
    key = db.last_upload_key
    assert key is not None
    if deleted:
        db.document = db.documents[0]
        await db.remove_document()
    intent = _columns(db.intents[0])
    deletes = db.storage.delete.await_count
    with pytest.raises(DocumentUploadAlreadyPublishedError):
        await _close(db, key)
    with pytest.raises(DocumentUploadClosureNotFoundError):
        await _closure(db, key)
    assert not db.closures and _columns(db.intents[0]) == intent
    assert (await _lookup(db, key)).document == published
    assert await db.upload(upload_key=key) == published
    assert await _usage(db) == 5 and db.storage.delete.await_count == deletes
    db.storage.put.assert_awaited_once()


@pytest.mark.parametrize("exists", [False, True])
async def test_missing_closure_is_not_a_storage_or_directory_guess(exists: bool) -> None:
    """未受付/未停止は独立 404 で、同名や blob の現存を原停止の証明に使わない。"""

    db = UploadDatabase()
    key = await _leave_pending(db) if exists else uuid4()
    with pytest.raises(DocumentUploadClosureNotFoundError):
        await _closure(db, key)
    if not exists:
        with pytest.raises(DocumentUploadNotFoundError):
            await _close(db, key)
    assert not db.closures
    assert db.storage.put.await_count == int(exists)
    db.storage.get.assert_not_called()
    db.storage.delete.assert_not_awaited()


async def test_new_session_can_close_then_read_archived_without_csrf() -> None:
    """新会話は人工停止/履歴読取だけを行い、元 PUT の会話・request を接管しない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    original_session = db.intents[0].original_session_id
    old_access = db.access
    _login_again(db)
    closed, created = await _close(db, key)
    assert created and db.closures[0].session_id == db.auth_session.id != original_session
    assert db.intents[0].original_session_id == original_session
    db.project.status = "ARCHIVED"
    db.access = replace(db.access, csrf_token="read-needs-no-csrf")
    assert await _closure(db, key) == closed
    with pytest.raises(CsrfRejectedError):
        await _close(db, key)
    db.access = replace(db.access, csrf_token=old_access.csrf_token)
    with pytest.raises(UnauthorizedSessionError):
        await db.document_service.get_upload_closure(
            project_id=db.project.id,
            upload_key=key,
            access=old_access,
        )
    assert len(db.closures) == 1 and not db.documents


@pytest.mark.parametrize("operation", ["close", "replay", "lookup"])
@pytest.mark.parametrize(
    "reason",
    [
        "disabled",
        "user_missing",
        "session_missing",
        "new_session_only",
        "revoked",
        "expired",
        "absolute_expired",
        "role_changed",
        "project_missing",
        "foreign_project",
        "member_missing",
        "member_removed",
    ],
)
async def test_closure_operations_recheck_current_qualification(
    operation: str, reason: str
) -> None:
    """旧停止の再表示も原 user/会話/Project/所属の現在資格を省略しない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    if operation != "close":
        await _close(db, key)
    original = [_columns(row) for row in db.closures]
    expected = deny(db, reason)
    with pytest.raises(expected):
        if operation == "lookup":
            await _closure(db, key)
        else:
            await _close(db, key)
    assert [_columns(row) for row in db.closures] == original
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("already_closed", [False, True])
async def test_archived_project_forbids_close_and_replay_but_allows_history(
    already_closed: bool,
) -> None:
    """停止も unsafe write であるため ACTIVE を必須にし、GET の帰档許可を移さない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    if already_closed:
        await _close(db, key)
    db.project.status = "ARCHIVED"
    with pytest.raises(ProjectArchivedError):
        await _close(db, key)
    if already_closed:
        assert (await _closure(db, key)).publication_state == "CLOSED"
    else:
        with pytest.raises(DocumentUploadClosureNotFoundError):
            await _closure(db, key)
    assert len(db.closures) == int(already_closed)


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("already_closed", [False, True])
async def test_another_actor_cannot_close_or_confirm_original_key(
    admin: bool, already_closed: bool
) -> None:
    """Project の ADMIN 読取権でも別 actor の原 upload の所有権を付与しない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    if already_closed:
        await _close(db, key)
    user_id = uuid4()
    db.user.id = db.auth_session.user_id = user_id
    assert db.member is not None
    db.member.user_id = user_id
    role = "ADMIN" if admin else "USER"
    db.user.system_role = db.auth_session.system_role_at_login = role
    db.access = replace(
        db.access, actor=replace(db.access.actor, user_id=user_id, system_role=role)
    )
    with pytest.raises(DocumentUploadNotFoundError):
        await _close(db, key)
    with pytest.raises(DocumentUploadClosureNotFoundError):
        await _closure(db, key)
    assert len(db.closures) == int(already_closed)
    db.storage.put.assert_awaited_once()


@pytest.mark.parametrize("persists", [False, True])
async def test_unknown_close_commit_is_confirmed_only_by_original_closure(persists: bool) -> None:
    """同じ commit 例外でも実持続差を GET だけで確認し、PUT/DELETE/自動再停止をしない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    original = _columns(db.intents[0])
    db.commit_error, db.commit_persists = True, persists
    with pytest.raises(ConnectionError):
        await _close(db, key)
    assert len(db.closures) == int(persists)
    assert (db.intents[0].publication_closed_at is not None) is persists
    _restart(db)
    if persists:
        assert (await _closure(db, key)).closed_at == db.closures[0].closed_at
    else:
        assert _columns(db.intents[0]) == original
        with pytest.raises(DocumentUploadClosureNotFoundError):
            await _closure(db, key)
    assert await _usage(db) == 5 and (await _lookup(db, key)).state == "PENDING"
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


async def test_cancelled_close_commit_rolls_back_marker_and_audit_together() -> None:
    """この fake の未 commit 取消は両行を戻すが、現実の commit 未知を一般に否定しない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    original = _columns(db.intents[0])
    db.commit_entered.clear()
    db.commit_release = asyncio.Event()
    task = asyncio.create_task(_close(db, key))
    try:
        await asyncio.wait_for(db.commit_entered.wait(), 2)
        assert len(db.closures) == 1 and db.intents[0].publication_closed_at is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _restart(db)
    assert not db.closures and _columns(db.intents[0]) == original
    assert await _usage(db) == 5
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("failure", ["constraint", "connection", "cancelled"])
async def test_failed_close_flush_restores_marker_and_audit_without_remote_compensation(
    failure: str,
) -> None:
    """停止監査の制約/切断/取消は原意図と同時 rollback し、成功回执や storage 清理へ変えない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    original = _columns(db.intents[0])
    error: BaseException = {
        "constraint": IntegrityError("Synthetic closure INSERT", {}, RuntimeError("Constraint")),
        "connection": ConnectionError("Synthetic flush unavailable"),
        "cancelled": asyncio.CancelledError(),
    }[failure]

    def reject_flush() -> None:
        """marker と監査の両方が追加された最終 flush でだけ失敗を注入する。"""

        assert db.intents[0].publication_closed_at is not None and len(db.closures) == 1
        raise error

    db.on_flush = reject_flush
    with pytest.raises(type(error)) as caught:
        await _close(db, key)
    assert caught.value is error
    assert not db.closures and _columns(db.intents[0]) == original
    _restart(db)
    with pytest.raises(DocumentUploadClosureNotFoundError):
        await _closure(db, key)
    assert await _usage(db) == 5 and await db.blobs.get(db.intents[0].storage_key) == b"hello"
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


async def test_concurrent_same_key_closures_share_gate_and_keep_one_original_audit() -> None:
    """後発停止が先発 commit を越えず、同じ原監査を非新規として返す合成競争を検証する。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    start = db.transactions
    db.commit_entered.clear()
    release = db.commit_release = asyncio.Event()
    first = asyncio.create_task(_close(db, key))
    second: asyncio.Task[tuple[StoredDocumentUploadClosure, bool]] | None = None
    try:
        await asyncio.wait_for(db.commit_entered.wait(), 2)
        second = asyncio.create_task(_close(db, key))
        await asyncio.sleep(0)
        assert not second.done() and db.transactions == start + 1
        release.set()
        first_result, second_result = await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert first_result[1] is True and second_result == (first_result[0], False)
    finally:
        release.set()
        tasks = [first, *([] if second is None else [second])]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len(db.closures) == 1 and not db.documents
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("relogin", [False, True])
async def test_closed_commit_blocks_late_original_put_from_publishing(relogin: bool) -> None:
    """PUT は止まらなくても、停止と TX B が同じ gate で直列化され元目録を公開しない。"""

    db = UploadDatabase()
    key = uuid4()
    put_entered, put_release = asyncio.Event(), asyncio.Event()

    async def pending_put(storage_key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """原 byte 保存後の待機を保持し、別停止 transaction の後に元結果を返す。"""

        blob = await db.put_blob(storage_key, data, content_type=content_type)
        put_entered.set()
        await put_release.wait()
        return blob

    db.storage.put.side_effect = pending_put
    upload = asyncio.create_task(db.upload(upload_key=key))
    closure: asyncio.Task[tuple[StoredDocumentUploadClosure, bool]] | None = None
    close_release = asyncio.Event()
    try:
        await asyncio.wait_for(put_entered.wait(), 2)
        if relogin:
            _login_again(db)
        db.commit_entered.clear()
        db.commit_release = close_release
        closure = asyncio.create_task(_close(db, key))
        await asyncio.wait_for(db.commit_entered.wait(), 2)
        put_release.set()
        await asyncio.sleep(0)
        assert not upload.done() and not db.documents
        close_release.set()
        closed, created = await asyncio.wait_for(closure, 2)
        assert created and closed.upload_key == key
        with pytest.raises(UnauthorizedSessionError if relogin else DocumentUploadClosedError):
            await asyncio.wait_for(upload, 2)
    finally:
        put_release.set()
        close_release.set()
        tasks = [upload, *([] if closure is None else [closure])]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not db.documents and len(db.closures) == 1
    assert (await _lookup(db, key)).state == "PENDING" and await _usage(db) == 5
    assert await db.blobs.get(db.intents[0].storage_key) == b"hello"
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


async def test_publisher_commit_wins_race_without_closure_or_delete() -> None:
    """先行 TX B の公開を後発停止が取消さず、原公開回执を残して明示競合にする。"""

    db = UploadDatabase()
    key = uuid4()
    release = asyncio.Event()

    def wait_for_publish_commit() -> None:
        """TX A ではなく TX B の commit にだけ明示 barrier を置く。"""

        db.commit_entered.clear()
        db.commit_release = release

    db.on_put = wait_for_publish_commit
    upload = asyncio.create_task(db.upload(upload_key=key))
    closure: asyncio.Task[tuple[StoredDocumentUploadClosure, bool]] | None = None
    try:
        await asyncio.wait_for(db.commit_entered.wait(), 2)
        assert db.transactions == 2 and db.intents[0].state == "PUBLISHED"
        closure = asyncio.create_task(_close(db, key))
        await asyncio.sleep(0)
        assert not closure.done() and db.transactions == 2
        release.set()
        published = await asyncio.wait_for(upload, 2)
        with pytest.raises(DocumentUploadAlreadyPublishedError):
            await asyncio.wait_for(closure, 2)
        assert (await _lookup(db, key)).document == published
    finally:
        release.set()
        tasks = [upload, *([] if closure is None else [closure])]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not db.closures and len(db.documents) == 1
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize(
    ("operation", "boundary"),
    [
        (operation, boundary)
        for operation in ("close", "replay", "lookup", "missing")
        for boundary in (
            Organization,
            User,
            AuthSession,
            Project,
            ProjectMember,
            ProjectDocumentUpload,
            "closure",
            "flush",
        )
        if (operation, boundary) != ("missing", "flush")
    ],
)
async def test_final_qualification_uses_fresh_clock_with_unchanged_original_expiry(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    boundary: type[Any] | str,
) -> None:
    """全 lock/監査読取/flush 待機後に時計を進め、固定 expiry の失効が成功/404より優先する。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    if operation in {"replay", "lookup"}:
        await _close(db, key)
    original_closures = [_columns(row) for row in db.closures]
    original = _columns(db.intents[0])
    expiry = db.auth_session.idle_expires_at
    advanced = expiry + timedelta(seconds=1)

    class AfterWaitClock(datetime):
        """service の現在時刻だけを進め、既に锁定した session field は書き換えない。"""

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> AfterWaitClock:
            """DB 待機後の新しい期限判定を無 sleep で再現する。"""

            return cls.fromtimestamp(advanced.timestamp(), tz=tz)

    def advance() -> None:
        """指定した待機点だけで、業務 service が読む時計を差し替える。"""

        monkeypatch.setattr("skillmind.documents.service.datetime", AfterWaitClock)

    if boundary == "flush":
        db.on_flush = advance
    elif boundary == "closure":
        db.on_closure_read = advance
    else:
        db.on_lock = lambda entity: advance() if entity is boundary else None
    with pytest.raises(UnauthorizedSessionError):
        if operation in {"lookup", "missing"}:
            await _closure(db, key)
        else:
            await _close(db, key)
    assert db.auth_session.idle_expires_at == expiry
    assert _columns(db.intents[0]) == original
    assert [_columns(row) for row in db.closures] == original_closures
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize(
    "field",
    [
        "upload_key",
        "document_id",
        "storage_namespace_id",
        "storage_descriptor_checksum",
        "storage_key",
        "checksum",
        "original_request_id",
        "original_session_id",
        "created_at",
        "storage_is_durable",
    ],
)
async def test_changed_closed_target_never_becomes_a_trusted_confirmation(field: str) -> None:
    """形式上有効な原 key/namespace/ID の改変も独立 binding で拒否し、現在設定で補正しない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    await _close(db, key)
    intent = db.intents[0]
    values: dict[str, Any] = {
        "upload_key": uuid4(),
        "document_id": uuid4(),
        "storage_namespace_id": uuid4(),
        "storage_descriptor_checksum": "sha256:" + "0" * 64,
        "storage_key": "unrelated-safe-key",
        "checksum": "sha256:" + "0" * 64,
        "original_request_id": uuid4(),
        "original_session_id": uuid4(),
        "created_at": intent.created_at - timedelta(seconds=1),
        "storage_is_durable": not intent.storage_is_durable,
    }
    setattr(intent, field, values[field])
    if field == "document_id":
        intent.storage_key = document_storage_key(
            intent.project_id, intent.document_id, intent.name
        )
    key = intent.upload_key
    before = [_columns(row) for row in db.closures]
    with pytest.raises(DocumentUploadInvalidError):
        await _closure(db, key)
    with pytest.raises(DocumentUploadInvalidError):
        await _close(db, key)
    assert [_columns(row) for row in db.closures] == before and not db.documents
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize(
    "damage",
    [
        "audit-missing",
        "marker-missing",
        "audit-hash",
        "request",
        "time",
        "audit-id",
        "session",
    ],
)
async def test_marker_and_audit_must_form_one_verified_original_closure(damage: str) -> None:
    """片方だけ残る状態や私有監査の改変を停止成功/未停止のどちらへも正常化しない。"""

    db = UploadDatabase()
    key = await _leave_pending(db)
    await _close(db, key)
    if damage == "audit-missing":
        db.closures = []
    elif damage == "marker-missing":
        db.intents[0].publication_closed_at = None
    elif damage == "audit-hash":
        db.closures[0].binding_checksum = "sha256:" + "0" * 64
    elif damage == "request":
        db.closures[0].request_id = uuid4()
    elif damage == "audit-id":
        db.closures[0].id = uuid4()
    elif damage == "session":
        db.closures[0].session_id = uuid4()
    else:
        db.closures[0].closed_at += timedelta(seconds=1)
    with pytest.raises(DocumentUploadInvalidError):
        await _closure(db, key)
    with pytest.raises(DocumentUploadInvalidError):
        await _lookup(db, key)
    with pytest.raises(DocumentUploadInvalidError):
        await db.upload(upload_key=key)
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("operation", ["close", "lookup"])
@pytest.mark.parametrize("key", [None, "", "invalid", UUID(int=0), 1, True])
async def test_closure_requires_non_nil_original_uuid_before_transactions(
    operation: str, key: object
) -> None:
    """HTTP 外からも UUID 型と原 key を必須にし、欠落を新しい要求へ自動補完しない。"""

    db = UploadDatabase()
    with pytest.raises((TypeError, ValueError)):
        if operation == "close":
            await _close(db, cast(UUID, key))
        else:
            await _closure(db, cast(UUID, key))
    assert not db.intents and not db.closures and db.transactions == 0
    db.storage.put.assert_not_awaited()
    db.storage.delete.assert_not_awaited()
