"""upload の原資格、quota、receipt と取消を SQL/commit fake で検証する。実 DB 証明ではない。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from projectmind.auth.domain import generate_session_credentials
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.core.hashing import sha256_hex
from projectmind.db.models import (
    AuthSession,
    Organization,
    Project,
    ProjectDocumentUpload,
    ProjectMember,
    User,
)
from projectmind.documents.domain import DocumentConflictError
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from projectmind.storage import FileStorageError, StoredBlob, UploadLimits, UploadRejectedError
from tests.documents.upload_harness import UploadDatabase


class ConstraintFailure(Exception):
    """Driver の公開 constraint metadata だけを持つ合成制約エラー。"""

    constraint_name: str | None = None


def expire(db: UploadDatabase) -> None:
    """実時計の sleep を使わず、原会話の期限だけを既に過ぎた値にする。"""

    db.auth_session.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)


def deny(db: UploadDatabase, reason: str) -> type[Exception]:
    """同じ拒否原因を PUT の前後へ注入し、別の新会話で原資格を救済しない。"""

    if reason == "disabled":
        db.user.status = "DISABLED"
    elif reason == "user_missing":
        db.users = []
    elif reason == "session_missing":
        db.auth_sessions = []
    elif reason == "new_session_only":
        credentials = generate_session_credentials()
        db.auth_session.token_hash = credentials.session_token_hash
        db.auth_session.csrf_token_hash = credentials.csrf_token_hash
    elif reason == "revoked":
        db.auth_session.revoked_at = datetime.now(UTC)
    elif reason == "expired":
        expire(db)
    elif reason == "absolute_expired":
        db.auth_session.absolute_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif reason == "role_changed":
        db.user.system_role = "ADMIN"
    elif reason == "csrf":
        db.access = replace(db.access, csrf_token="synthetic-rejected-csrf")
        return CsrfRejectedError
    elif reason == "project_missing":
        db.project_present = False
        return ProjectNotFoundError
    elif reason == "foreign_project":
        db.project.organization_id = uuid4()
        return ProjectNotFoundError
    elif reason == "archived":
        db.project.status = "ARCHIVED"
        return ProjectArchivedError
    elif reason == "member_missing":
        db.member = None
        return ProjectNotFoundError
    elif reason == "member_removed":
        assert db.member is not None
        db.member.status = "REMOVED"
        return ProjectNotFoundError
    else:
        raise AssertionError("Unknown synthetic denial")
    return UnauthorizedSessionError


async def test_upload_rechecks_original_access_in_two_short_transactions() -> None:
    """両 transaction は共有 lock 順序に従い、PUT は初回 commit 後だけ実行する。"""

    db = UploadDatabase()
    stored = await db.upload()
    assert db.lock_events == [
        Organization, User, AuthSession, Project, ProjectMember, ProjectDocumentUpload,
    ] * 2
    assert db.commits == 2 and db.rollbacks == 0 and db.usage_reads == 2
    assert stored.uploaded_by == db.access.actor.user_id
    assert stored.checksum == f"sha256:{sha256_hex(b'hello')}"
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


async def test_current_admin_does_not_require_project_membership() -> None:
    """有効な現 ADMIN の組織権限を、欠落 membership と混同しない。"""

    db = UploadDatabase()
    db.user.system_role = db.auth_session.system_role_at_login = "ADMIN"
    db.access = replace(db.access, actor=replace(db.access.actor, system_role="ADMIN"))
    db.member = None
    await db.upload()
    assert db.lock_events == [Organization, User, AuthSession, Project, ProjectDocumentUpload] * 2
    assert db.commits == 2


@pytest.mark.parametrize("after_put", [False, True])
@pytest.mark.parametrize("reason", [
    "disabled", "user_missing", "session_missing", "new_session_only", "revoked",
    "expired", "absolute_expired", "role_changed", "project_missing", "foreign_project",
    "archived", "member_missing", "member_removed",
])
async def test_stale_qualification_never_publishes_metadata(reason: str, after_put: bool) -> None:
    """PUT 後の撤権/削除/アーカイブでも metadata を保存せず、既存 byte は補償削除しない。"""

    db = UploadDatabase()
    expected: dict[str, type[Exception]] = {
        "project_missing": ProjectNotFoundError,
        "foreign_project": ProjectNotFoundError,
        "archived": ProjectArchivedError,
        "member_missing": ProjectNotFoundError,
        "member_removed": ProjectNotFoundError,
    }
    if after_put:
        def revoke_after_put() -> None:
            """初回 transaction の資格を保持したまま、PUT 後の事実だけを失効させる。"""

            deny(db, reason)

        db.on_put = revoke_after_put
    else:
        deny(db, reason)
    with pytest.raises(expected.get(reason, UnauthorizedSessionError)):
        await db.upload()
    assert not db.documents and db.rollbacks == 1
    assert db.commits == int(after_put)
    assert len(db.intents) == int(after_put)
    assert all(intent.state == "PENDING" and intent.size == 5 for intent in db.intents)
    if after_put:
        db.storage.put.assert_awaited_once()
        assert await db.blobs.get(db.storage.put.call_args.args[0]) == b"hello"
    else:
        db.storage.put.assert_not_awaited()
    db.storage.delete.assert_not_awaited()


async def test_original_csrf_is_required_before_storage() -> None:
    """現在 user と session が有効でも、別の CSRF を書込資格として受け入れない。"""

    db = UploadDatabase()
    deny(db, "csrf")
    with pytest.raises(CsrfRejectedError):
        await db.upload()
    db.storage.put.assert_not_awaited()


async def test_new_valid_session_does_not_replace_revoked_original_session() -> None:
    """同じ user に新会話があっても、保存処理は PUT 前からの原 cookie だけを再検証する。"""

    db = UploadDatabase()

    def login_again() -> None:
        """新しい有効会話を追加しても元リクエストの資格を更新しない。"""

        credentials = generate_session_credentials()
        new_session = deepcopy(db.auth_session)
        new_session.id = uuid4()
        new_session.token_hash = credentials.session_token_hash
        new_session.csrf_token_hash = credentials.csrf_token_hash
        db.auth_sessions.append(new_session)
        db.auth_session.revoked_at = datetime.now(UTC)
        db.access = replace(
            db.access, session_token=credentials.session_token, csrf_token=credentials.csrf_token
        )

    db.on_put = login_again
    with pytest.raises(UnauthorizedSessionError):
        await db.upload()
    assert not db.documents and db.transactions == 2
    db.storage.delete.assert_not_awaited()


async def test_mutating_callers_access_reference_does_not_change_captured_actor() -> None:
    """呼出側が別 access に切替えても、upload は最初の不変 UserAccess で完結する。"""

    db = UploadDatabase()
    original = db.access

    def replace_caller_access() -> None:
        """別 user の参照へ切替えるが、service に渡した原 access 自体は不変のまま保つ。"""

        db.access = replace(db.access, actor=replace(db.access.actor, user_id=uuid4()))

    db.on_put = replace_caller_access
    stored = await db.upload()
    assert stored.uploaded_by == original.actor.user_id
    assert stored.uploaded_by != db.access.actor.user_id


@pytest.mark.parametrize("transaction", [1, 2])
@pytest.mark.parametrize("wait_at", [Organization, User, AuthSession, Project, ProjectMember])
async def test_expiry_during_each_lock_wait_rolls_back(
    transaction: int, wait_at: type[Any],
) -> None:
    """各 lock 待機後は新時刻で原会話を確認し、入口時刻で期限切れを通さない。"""

    db = UploadDatabase()

    def after_lock(entity: type[Any]) -> None:
        """対象 transaction の一つの lock 待機だけで期限を進める。"""

        if db.transactions == transaction and entity is wait_at:
            expire(db)

    db.on_lock = after_lock
    with pytest.raises(UnauthorizedSessionError):
        await db.upload()
    assert not db.documents and db.commits == transaction - 1 and db.rollbacks == 1
    assert len(db.intents) == transaction - 1
    assert db.storage.put.await_count == transaction - 1
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("transaction", [1, 2])
@pytest.mark.parametrize("boundary", ["usage", "flush"])
async def test_expiry_after_usage_or_final_flush_rolls_back(
    transaction: int, boundary: str,
) -> None:
    """quota SELECT と最終 flush の await も認証期限の最終判定より前に置く。"""

    db = UploadDatabase()

    def after_wait() -> None:
        """選んだ待機点だけを失効させ、成功側 transaction の commit は残す。"""

        if db.transactions == transaction:
            expire(db)

    if boundary == "usage":
        db.on_usage = after_wait
    else:
        db.on_flush = after_wait
    with pytest.raises(UnauthorizedSessionError):
        await db.upload()
    assert not db.documents and db.commits == transaction - 1 and db.rollbacks == 1
    assert len(db.intents) == transaction - 1
    assert db.storage.put.await_count == transaction - 1
    db.storage.delete.assert_not_awaited()


async def test_authorization_loss_takes_priority_over_quota_rejection() -> None:
    """用量 SELECT 中に原資格が失効した場合、quota 拒否で認証結果を隠さない。"""

    db = UploadDatabase(limits=UploadLimits(10, 10, frozenset({"text/plain"})))
    db.document.size = 10
    db.documents = [db.document]
    db.on_usage = lambda: expire(db)
    with pytest.raises(UnauthorizedSessionError):
        await db.upload()
    db.storage.put.assert_not_awaited()


@pytest.mark.parametrize("transaction", [1, 2])
async def test_expired_session_takes_priority_over_missing_project(transaction: int) -> None:
    """Project lock 待機で消失と失効が重なっても、原資格失効を 404 で隠さない。"""

    db = UploadDatabase()

    def disappear_after_wait(entity: type[Any]) -> None:
        """二つの境界の同時変化を、Project SELECT が返る直前に注入する。"""

        if db.transactions == transaction and entity is Project:
            db.project_present = False
            expire(db)

    db.on_lock = disappear_after_wait
    with pytest.raises(UnauthorizedSessionError):
        await db.upload()
    assert not db.documents and db.storage.put.await_count == transaction - 1
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("competing_size,accepted", [(5, True), (6, False)])
async def test_final_quota_uses_metadata_published_during_put(
    competing_size: int, accepted: bool,
) -> None:
    """元予約を再加算せず、PUT 中に旧 writer が追加した metadata も最新占用へ含める。"""

    db = UploadDatabase(limits=UploadLimits(10, 10, frozenset({"text/plain"})))
    db.document.size = competing_size
    db.on_put = lambda: db.documents.append(db.document)
    if accepted:
        await db.upload()
        assert sum(item.size for item in db.documents) == 10
    else:
        with pytest.raises(UploadRejectedError) as caught:
            await db.upload()
        assert caught.value.code == "project_quota_exceeded"
        assert db.documents == [db.document]
        assert await db.blobs.get(db.storage.put.call_args.args[0]) == b"hello"
    assert db.usage_reads == 2
    db.storage.delete.assert_not_awaited()


async def test_quota_does_not_count_other_projects() -> None:
    """同じ用量 query が別 Project の metadata を取り込まないことを SQL 条件で守る。"""

    db = UploadDatabase(limits=UploadLimits(5, 5, frozenset({"text/plain"})))
    db.document.project_id = uuid4()
    db.document.size = 100
    db.documents = [db.document]
    await db.upload()
    assert len(db.documents) == 2 and db.commits == 2


async def test_mutable_input_is_copied_before_any_await() -> None:
    """呼出元 buffer が最初の lock 待機で変わっても、検査/PUT/hash は元 byte を共有する。"""

    db = UploadDatabase()
    data = bytearray(b"hello")

    def mutate(_: type[Any]) -> None:
        """本文を伸ばしても service のスナップショットに影響させない。"""

        data[:] = b"api_key=synthetic-not-uploaded"

    db.on_lock = mutate
    stored = await db.upload(data=data)  # type: ignore[arg-type]
    assert stored.size == 5 and stored.checksum == f"sha256:{sha256_hex(b'hello')}"
    assert await db.blobs.get(db.documents[0].storage_key) == b"hello"


@pytest.mark.parametrize("field,value", [
    ("key", "different-safe-key"), ("size", 6), ("size", True),
    ("content_type", "image/png"), ("sha256", "sha256:" + "0" * 64),
])
async def test_mismatched_storage_acknowledgement_never_publishes_or_deletes(
    field: str, value: object,
) -> None:
    """adapter receipt を盲信して metadata を作らず、不一致でも所有未確定 blob は消さない。"""

    db = UploadDatabase()

    async def bad_receipt(key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """byte は保存した上で一つの receipt 項目だけを偽装する。"""

        blob = await db.put_blob(key, data, content_type=content_type)
        if field == "size":
            assert isinstance(value, int)
            return replace(blob, size=value)
        assert isinstance(value, str)
        if field == "key":
            return replace(blob, key=value)
        if field == "content_type":
            return replace(blob, content_type=value)
        assert field == "sha256"
        return replace(blob, sha256=value)

    db.storage.put.side_effect = bad_receipt
    with pytest.raises(FileStorageError, match="acknowledgement"):
        await db.upload(data=b"x" if value is True else b"hello")
    assert not db.documents and db.transactions == 1 and db.commits == 1
    assert len(db.intents) == 1 and db.intents[0].state == "PENDING"
    assert await db.blobs.exists(db.storage.put.call_args.args[0])
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("name_size", [129, 150, 200])
async def test_storage_segment_limit_rejects_long_names_before_transactions(name_size: int) -> None:
    """公開名 200 と storage 単段 128 の不一致は共有規則から安定拒否へ変換する。"""

    db = UploadDatabase()
    with pytest.raises(UploadRejectedError) as caught:
        await db.upload(name="文" * name_size)
    assert caught.value.code == "invalid_document_name"
    assert db.transactions == 0
    db.storage.put.assert_not_awaited()


async def test_shared_path_validation_and_size_property_do_not_truncate_names() -> None:
    """route が本文前に同じ上限/純粋 path 検証を使え、128 文字の原名は保持される。"""

    db = UploadDatabase(limits=UploadLimits(5, 10, frozenset({"text/plain"})))
    assert db.document_service.max_upload_bytes == 5
    assert db.document_service.validate_upload_path(
        project_id=db.project.id, folder=" specs/ ", name="文" * 128,
    ) == ("specs", "文" * 128)
    stored = await db.upload(name="文" * 128)
    assert stored.name == "文" * 128
    assert db.documents[0].storage_key.endswith("文" * 128)


@pytest.mark.parametrize("payload,code", [(b"", "empty_upload"), (b"123456", "file_too_large")])
async def test_exact_actual_byte_limit_rejects_before_any_storage(
    payload: bytes, code: str,
) -> None:
    """metadata 申告や receipt の値でなく保存する実 byte で一文書上限を判定する。"""

    db = UploadDatabase(limits=UploadLimits(5, 10, frozenset({"text/plain"})))
    with pytest.raises(UploadRejectedError) as caught:
        await db.upload(data=payload)
    assert caught.value.code == code and db.transactions == 0
    db.storage.put.assert_not_awaited()


@pytest.mark.parametrize("constraint", ["uq_project_documents_project_folder_name", "other", None])
async def test_only_exact_document_path_constraint_becomes_conflict(constraint: str | None) -> None:
    """制約本文の文字列一致を使わず、未知 IntegrityError は確定した同名拒否に変えない。"""

    db = UploadDatabase()
    original = ConstraintFailure("uq_project_documents_project_folder_name")
    original.constraint_name = constraint
    error = IntegrityError("Synthetic statement", {}, original)

    def fail_flush() -> None:
        """INSERT 制約が評価される最終 flush の失敗を注入する。"""

        if db.transactions == 2:
            raise error

    db.on_flush = fail_flush
    expected = (
        DocumentConflictError
        if constraint == "uq_project_documents_project_folder_name" else IntegrityError
    )
    with pytest.raises(expected) as caught:
        await db.upload()
    if expected is IntegrityError:
        assert caught.value is error
    assert not db.documents and db.commits == 1 and db.rollbacks == 1
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("constraint", ["uq_project_documents_project_folder_name", "other", None])
async def test_failed_publish_flush_rechecks_fixed_session_expiry_using_new_clock(
    monkeypatch: pytest.MonkeyPatch, constraint: str | None,
) -> None:
    """元 expiry を改変せず、失敗 flush 待機で進んだ現在時刻を確定拒否の前に再評価する。"""

    db = UploadDatabase()
    original_expiry = db.auth_session.idle_expires_at
    after_expiry = original_expiry + timedelta(seconds=1)
    original = ConstraintFailure("uq_project_documents_project_folder_name")
    original.constraint_name = constraint
    failure = IntegrityError("Synthetic failed flush", {}, original)

    class AfterWaitClock(datetime):
        """service の時計だけを進め、認可元 row の凍結した expiry を維持する。"""

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> AfterWaitClock:
            """実 sleep や session field 書換えなしで、待機後の新時刻を返す。"""

            return cls.fromtimestamp(after_expiry.timestamp(), tz=tz)

    def fail_after_wait() -> None:
        """TX B の flush だけを DB 拒否にし、応答分類時には元会話が期限切れとなる。"""

        if db.transactions == 2:
            monkeypatch.setattr("projectmind.documents.service.datetime", AfterWaitClock)
            raise failure

    db.on_flush = fail_after_wait
    expected = (
        UnauthorizedSessionError
        if constraint == "uq_project_documents_project_folder_name" else IntegrityError
    )
    with pytest.raises(expected) as caught:
        await db.upload()
    if expected is IntegrityError:
        assert caught.value is failure
    assert db.auth_session.idle_expires_at == original_expiry
    assert not db.documents and len(db.intents) == 1 and db.intents[0].state == "PENDING"
    assert not db.cleanups and db.commits == 1 and db.rollbacks == 1
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("transaction", [1, 2])
@pytest.mark.parametrize("persists", [False, True])
async def test_unknown_commit_never_retries_put_or_compensates(
    transaction: int, persists: bool,
) -> None:
    """commit 応答喪失は持続/非持続のどちらでも未知のまま伝播し、blob を消さない。"""

    db = UploadDatabase()

    def lose_acknowledgement() -> None:
        """選んだ commit だけに親 fake の持続境界を適用する。"""

        db.commit_error = True
        db.commit_persists = persists

    if transaction == 1:
        lose_acknowledgement()
    else:
        db.on_put = lose_acknowledgement
    with pytest.raises(ConnectionError, match="acknowledgement"):
        await db.upload()
    assert len(db.documents) == int(transaction == 2 and persists)
    assert db.storage.put.await_count == transaction - 1
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("error_factory", [ConnectionError, asyncio.CancelledError])
async def test_put_unknown_or_cancelled_after_write_does_not_delete(
    error_factory: Callable[[], BaseException],
) -> None:
    """書込後の切断/取消を blob 不在とみなさず、metadata 再試行や補償削除をしない。"""

    db = UploadDatabase()

    async def lose_put_result(key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """遠端相当の byte 保存後に結果だけを失う。"""

        await db.put_blob(key, data, content_type=content_type)
        raise error_factory()

    db.storage.put.side_effect = lose_put_result
    with pytest.raises((ConnectionError, asyncio.CancelledError)):
        await db.upload()
    assert not db.documents and db.commits == 1 and db.transactions == 1
    assert await db.blobs.exists(db.storage.put.call_args.args[0])
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("transaction", [1, 2])
async def test_cancellation_while_commit_waits_never_puts_again_or_deletes(
    transaction: int,
) -> None:
    """commit 待機の task 取消でも再送/削除せず、実 DB 成否はこの fake から推測しない。"""

    db = UploadDatabase()

    def block_commit() -> None:
        """対象外 commit の通知を捨て、指定した commit の待機だけを制御する。"""

        db.commit_entered.clear()
        db.commit_release = asyncio.Event()

    if transaction == 1:
        block_commit()
    else:
        db.on_put = block_commit
    task = asyncio.create_task(db.upload())
    try:
        await asyncio.wait_for(db.commit_entered.wait(), timeout=2)
        assert db.transactions == transaction
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not db.documents and db.rollbacks == 1
    assert db.storage.put.await_count == transaction - 1
    if transaction == 2:
        assert await db.blobs.exists(db.storage.put.call_args.args[0])
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("access_arguments", [{}, {"access": None}, {"uploaded_by": uuid4()}])
async def test_upload_cannot_accept_forged_uploader_or_missing_access(
    access_arguments: dict[str, Any],
) -> None:
    """呼出元の UUID だけで upload を認可する旧 signature を復活させない。"""

    db = UploadDatabase()
    with pytest.raises(TypeError):
        await db.document_service.upload_document(
            project_id=db.project.id, folder="", name="note.txt", data=b"hello",
            content_type="text/plain", upload_key=uuid4(), **access_arguments,
        )
    assert db.transactions == 0
    db.storage.put.assert_not_awaited()
