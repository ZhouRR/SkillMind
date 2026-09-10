"""持続原要求、単一 dispatch、未知結果と清理占用を実 service と局部 DB fake で検証する。

再起動は service 再構築の seam であり、実 process/PG/遠端 writer 停止の証明ではない。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from skillmind.auth.domain import generate_session_credentials
from skillmind.auth.sessions import UnauthorizedSessionError
from skillmind.db.models import ProjectDocumentUpload
from skillmind.documents.domain import (
    DocumentConflictError,
    DocumentNotFoundError,
    DocumentUploadInvalidError,
    DocumentUploadKeyConflictError,
    DocumentUploadNotFoundError,
    DocumentUploadPendingError,
    StoredDocumentUpload,
)
from skillmind.documents.repository import DocumentRepository
from skillmind.documents.service import DocumentService
from skillmind.projects.domain import ProjectArchivedError
from skillmind.storage import FileStorageError, StoredBlob, UploadLimits, UploadRejectedError
from tests.documents.test_document_upload_authorization import deny, expire
from tests.documents.upload_harness import UPLOAD_LIMITS, UploadDatabase


async def _lookup(db: UploadDatabase, key: UUID | None = None) -> StoredDocumentUpload:
    """現在 actor と原 key を使い、目録や object の存在から公開結果を推測しない。"""

    key = key or db.last_upload_key
    assert key is not None
    return await db.document_service.get_upload(
        project_id=db.project.id, upload_key=key, access=db.access,
    )


def _restart(db: UploadDatabase) -> None:
    """永続相当行と adapter は保持し、process 内の service 状態だけを作り直す。"""

    db.commit_error = False
    db.commit_persists = False
    db.commit_release = None
    db.on_put = None
    db.on_flush = None
    db.on_lock = None
    db.document_service = DocumentService(
        db.session_factory, file_storage=db.storage, limits=UPLOAD_LIMITS,
    )


async def _leave_pending(db: UploadDatabase) -> UUID:
    """PUT が実 byte を保存した後に応答を失わせ、PENDING を残す。"""

    async def lose_receipt(key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """byte 保存と application に結果が届くことを区別する。"""

        await db.put_blob(key, data, content_type=content_type)
        raise FileStorageError("Synthetic PUT result unavailable")

    db.storage.put.side_effect = lose_receipt
    with pytest.raises(FileStorageError):
        await db.upload()
    assert db.last_upload_key is not None
    db.storage.put.side_effect = db.put_blob
    return db.last_upload_key


async def _usage(db: UploadDatabase) -> int:
    """同じ transaction seam で本番 quota SQL を実行し、test 独自の合計を答えにしない。"""

    async with db.transaction():
        return await DocumentRepository(db.session).project_usage_bytes(db.project.id)


async def test_reservation_commits_before_dispatch_and_publishes_without_double_charge() -> None:
    """PUT は持続済み原要求を観測し、公開は同じ意図・原 document と占用を引き継ぐ。"""

    db = UploadDatabase()
    key, original_request, original_session = uuid4(), db.access.request_id, db.auth_session.id

    def observe_pending() -> None:
        """lock 外の PUT 完了時点で原予約が既に committed かつ未公開であることを確かめる。"""

        assert db.commits == 1 and not db.documents and len(db.intents) == 1
        intent = db.intents[0]
        assert intent.state == "PENDING" and intent.upload_key == key
        assert intent.original_request_id == original_request
        assert intent.original_session_id == original_session
        assert intent.actor_id == db.access.actor.user_id and intent.size == 5
        assert intent.published_at is None and intent.cleanup_requested_at is None

    db.on_put = observe_pending
    document = await db.upload(upload_key=key)
    intent = db.intents[0]
    assert intent.state == "PUBLISHED" and intent.document_id == document.document_id
    assert db.documents[0].upload_intent_id == intent.id
    assert intent.published_at == document.created_at and intent.created_at <= intent.published_at
    assert await _usage(db) == 5
    receipt = await _lookup(db, key)
    assert receipt.document == document and receipt.state == "PUBLISHED"
    assert set(asdict(receipt)) == {"upload_key", "project_id", "state", "created_at", "document"}
    db.storage.put.assert_awaited_once()


@pytest.mark.parametrize("transaction", [1, 2])
@pytest.mark.parametrize("persists", [False, True])
async def test_commit_unknown_is_resolved_only_by_original_intent_after_service_restart(
    transaction: int, persists: bool,
) -> None:
    """応答喪失の実持続差を GET が区別し、PENDING は再 PUT せず公開回执だけを再利用する。"""

    db = UploadDatabase()
    key = uuid4()

    def lose_commit_result() -> None:
        """選んだ commit の保存有無を制御するが、service には同じ未知例外だけを返す。"""

        db.commit_error, db.commit_persists = True, persists

    if transaction == 1:
        lose_commit_result()
    else:
        db.on_put = lose_commit_result
    with pytest.raises(ConnectionError):
        await db.upload(upload_key=key)
    assert db.storage.put.await_count == transaction - 1
    assert len(db.intents) == int(transaction == 2 or persists)
    _restart(db)
    if transaction == 1 and not persists:
        with pytest.raises(DocumentUploadNotFoundError):
            await _lookup(db, key)
    else:
        receipt = await _lookup(db, key)
        if transaction == 2 and persists:
            assert receipt.state == "PUBLISHED" and receipt.document is not None
            assert await db.upload(upload_key=key) == receipt.document
        else:
            assert receipt.state == "PENDING" and receipt.document is None
            with pytest.raises(DocumentUploadPendingError):
                await db.upload(upload_key=key)
        assert await _usage(db) == 5
    assert db.storage.put.await_count == transaction - 1
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("transaction", [1, 2])
async def test_cancelled_commit_preserves_only_previously_committed_reservation(
    transaction: int,
) -> None:
    """取消は未知を再送せず、この fake の未 commit 差分だけを巻き戻して原予約を残す。"""

    db = UploadDatabase()
    key = uuid4()

    def block_commit() -> None:
        """対象 commit 以外の通知を除き、task cancellation を明示 barrier へ届ける。"""

        db.commit_entered.clear()
        db.commit_release = asyncio.Event()

    if transaction == 1:
        block_commit()
    else:
        db.on_put = block_commit
    task = asyncio.create_task(db.upload(upload_key=key))
    try:
        await asyncio.wait_for(db.commit_entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _restart(db)
    assert not db.documents and len(db.intents) == transaction - 1
    if transaction == 1:
        with pytest.raises(DocumentUploadNotFoundError):
            await _lookup(db, key)
    else:
        assert (await _lookup(db, key)).state == "PENDING"
        assert await _usage(db) == 5
        with pytest.raises(DocumentUploadPendingError):
            await db.upload(upload_key=key)
    assert db.storage.put.await_count == transaction - 1
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_unknown_put_keeps_billable_original_intent_and_never_retries(
    cancelled: bool,
) -> None:
    """遠端 byte が存在しても PENDING を成功扱いせず、再受付から二回目の PUT を作らない。"""

    db = UploadDatabase()

    async def lose_receipt(key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """保存後の切断と取消を同じ持続予約に帰属させる。"""

        await db.put_blob(key, data, content_type=content_type)
        if cancelled:
            raise asyncio.CancelledError()
        raise FileStorageError("Synthetic result unavailable")

    db.storage.put.side_effect = lose_receipt
    with pytest.raises((asyncio.CancelledError, FileStorageError)):
        await db.upload()
    original = db.intents[0]
    _restart(db)
    db.storage.namespace = None
    receipt = await _lookup(db)
    assert receipt.state == "PENDING" and receipt.document is None
    assert await _usage(db) == 5
    with pytest.raises(DocumentUploadPendingError):
        await db.upload(upload_key=original.upload_key)
    assert db.intents == [original] and not db.documents
    assert await db.blobs.get(original.storage_key) == b"hello"
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("change", ["data", "name", "folder", "mime"])
async def test_same_key_with_different_normalized_request_conflicts_without_dispatch(
    published: bool, change: str,
) -> None:
    """PENDING/PUBLISHED の双方で元本文/path/MIME が違えば、同一 operation と認めない。"""

    db = UploadDatabase()
    if published:
        await db.upload()
        assert db.last_upload_key is not None
        key = db.last_upload_key
    else:
        key = await _leave_pending(db)
    arguments: dict[str, object] = {"upload_key": key}
    changes: dict[str, dict[str, object]] = {
        "data": {"data": b"other"}, "name": {"name": "different.txt"},
        "folder": {"folder": "different"}, "mime": {"content_type": "image/png"},
    }
    arguments.update(changes[change])
    with pytest.raises(DocumentUploadKeyConflictError):
        await db.upload(**arguments)  # type: ignore[arg-type]
    assert len(db.intents) == 1
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


async def test_normalized_replay_needs_no_new_identity_or_current_storage() -> None:
    """等価な正規化入力だけを元回执に結び、新 request/session や現在 namespace を混ぜない。"""

    db = UploadDatabase()
    key = uuid4()
    document = await db.upload(upload_key=key, folder=" specs/ ", name=" note.txt ")
    original = deepcopy(db.intents[0])
    db.access = replace(db.access, request_id=uuid4())
    db.storage.namespace = None
    assert await db.upload(
        upload_key=key, folder="specs", name="note.txt", content_type="text/plain; charset=utf-8",
    ) == document
    assert db.intents[0].id == original.id
    assert db.intents[0].original_request_id == original.original_request_id
    assert db.intents[0].original_session_id == original.original_session_id
    assert db.usage_reads == 2 and len(db.intents) == len(db.documents) == 1
    db.storage.put.assert_awaited_once()


@pytest.mark.parametrize("policy", ["project-quota", "file-limit", "mime"])
async def test_post_replay_obeys_current_ingress_policy_but_get_preserves_original_receipt(
    policy: str,
) -> None:
    """再 POST は現受信 policy で拒否しても、原回执 GET は現在上限へ書き換えない。"""

    db = UploadDatabase()
    document = await db.upload()
    key = db.last_upload_key
    limits = {
        "project-quota": UploadLimits(10, 4, frozenset({"text/plain"})),
        "file-limit": UploadLimits(4, 10, frozenset({"text/plain"})),
        "mime": UploadLimits(10, 10, frozenset({"image/png"})),
    }[policy]
    db.document_service = DocumentService(
        db.session_factory, file_storage=db.storage, limits=limits,
    )
    transactions = db.transactions
    with pytest.raises(UploadRejectedError):
        await db.upload(upload_key=key)
    assert db.transactions == transactions
    assert (await _lookup(db, key)).document == document
    assert len(db.intents) == 1 and db.intents[0].size == 5
    db.storage.put.assert_awaited_once()


@pytest.mark.parametrize("same_key", [False, True])
async def test_inflight_reservation_blocks_second_dispatch_before_first_put_returns(
    same_key: bool,
) -> None:
    """lock 外で一回目 PUT が待っていても、同鍵/同名の二回目 writer は dispatch されない。"""

    db = UploadDatabase()
    entered, release = asyncio.Event(), asyncio.Event()
    key = uuid4()

    async def slow_put(storage_key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """確定した予約の byte を書いた後で結果を保留し、別 request を通す。"""

        result = await db.put_blob(storage_key, data, content_type=content_type)
        entered.set()
        await release.wait()
        return result

    db.storage.put.side_effect = slow_put
    first = asyncio.create_task(db.upload(upload_key=key))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(DocumentUploadPendingError if same_key else DocumentConflictError):
            await db.upload(upload_key=key if same_key else uuid4())
        assert len(db.intents) == 1 and not db.documents
        db.storage.put.assert_awaited_once()
        release.set()
        stored = await asyncio.wait_for(first, 2)
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert len(db.documents) == 1 and (await _lookup(db, key)).document == stored


async def test_distinct_inflight_path_cannot_spend_pending_upload_reservation() -> None:
    """まだ目録に無い占用も次の quota 判定へ含め、別名 PUT の並行過剰受付を防ぐ。"""

    db = UploadDatabase(limits=UploadLimits(10, 10, frozenset({"text/plain"})))
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_put(key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """一回目の外部 write だけを保留し、transaction lock は持たない。"""

        result = await db.put_blob(key, data, content_type=content_type)
        entered.set()
        await release.wait()
        return result

    db.storage.put.side_effect = slow_put
    first = asyncio.create_task(db.upload(name="one.txt", data=b"123456"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(UploadRejectedError) as caught:
            await db.upload(name="two.txt")
        assert caught.value.code == "project_quota_exceeded"
        assert len(db.intents) == 1 and db.intents[0].size == 6
        db.storage.put.assert_awaited_once()
        release.set()
        await asyncio.wait_for(first, 2)
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert await _usage(db) == 6


def _login_again(db: UploadDatabase) -> None:
    """旧会話を撤回して同 actor の新会話を作り、保存済み原 Session ID 自体は変更しない。"""

    credentials = generate_session_credentials()
    previous = db.auth_session
    current = deepcopy(previous)
    current.id = uuid4()
    current.token_hash = credentials.session_token_hash
    current.csrf_token_hash = credentials.csrf_token_hash
    previous.revoked_at = datetime.now(UTC)
    db.auth_session = current
    db.auth_sessions.append(current)
    db.access = replace(
        db.access, request_id=uuid4(), session_token=credentials.session_token,
        csrf_token=credentials.csrf_token,
    )


async def test_current_same_actor_can_read_original_receipt_after_relogin_and_archive() -> None:
    """GET は現資格で履歴を読み、原 cookie の復活や ACTIVE/CSRF write を要求しない。"""

    db = UploadDatabase()
    document = await db.upload()
    original_session = db.intents[0].original_session_id
    _login_again(db)
    db.project.status = "ARCHIVED"
    db.access = replace(db.access, csrf_token="synthetic-read-does-not-need-csrf")
    receipt = await _lookup(db)
    assert receipt.document == document and receipt.state == "PUBLISHED"
    assert db.intents[0].original_session_id == original_session != db.auth_session.id
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("admin", [False, True])
async def test_even_project_admin_cannot_query_another_actors_original_key(admin: bool) -> None:
    """Project 読取権と原受付の所有者を分け、他 actor の同じ UUID を列挙しない。"""

    db = UploadDatabase()
    await db.upload()
    user_id = uuid4()
    db.user.id = db.auth_session.user_id = user_id
    assert db.member is not None
    db.member.user_id = user_id
    role = "ADMIN" if admin else "USER"
    db.user.system_role = db.auth_session.system_role_at_login = role
    db.access = replace(
        db.access, actor=replace(db.access.actor, user_id=user_id, system_role=role),
    )
    with pytest.raises(DocumentUploadNotFoundError):
        await _lookup(db)
    db.storage.put.assert_awaited_once()


@pytest.mark.parametrize("reason", [
    "disabled", "session_missing", "revoked", "expired", "project_missing", "foreign_project",
    "member_missing", "member_removed",
])
async def test_original_receipt_lookup_still_requires_current_access(reason: str) -> None:
    """履歴確認が revoke/Project 不可視を迂回せず、storage の観察へ fallback しない。"""

    db = UploadDatabase()
    await db.upload()
    expected = deny(db, reason)
    with pytest.raises(expected):
        await _lookup(db)
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("boundary", ["intent-lock", "final-flush"])
@pytest.mark.parametrize("published", [False, True])
async def test_lookup_final_authorization_precedes_pending_or_published_receipt(
    boundary: str, published: bool,
) -> None:
    """保存済み回执でも lock/flush 後の失効を検出し、現在の利用資格を優先する。"""

    db = UploadDatabase()
    if published:
        await db.upload()
    else:
        await _leave_pending(db)
    if boundary == "intent-lock":
        db.on_lock = lambda entity: expire(db) if entity is ProjectDocumentUpload else None
    else:
        db.on_flush = lambda: expire(db)
    with pytest.raises(UnauthorizedSessionError):
        await _lookup(db)


@pytest.mark.parametrize("published", [False, True])
async def test_post_replay_requires_active_project_even_when_get_receipt_is_readable(
    published: bool,
) -> None:
    """履歴 GET の帰档許可を新 unsafe POST の許可と混同しない。"""

    db = UploadDatabase()
    if published:
        await db.upload()
    else:
        await _leave_pending(db)
    db.project.status = "ARCHIVED"
    assert (await _lookup(db)).state == ("PUBLISHED" if published else "PENDING")
    with pytest.raises(ProjectArchivedError):
        await db.upload(upload_key=db.last_upload_key)
    db.storage.put.assert_awaited_once()


async def test_unknown_key_lookup_is_not_a_same_name_or_storage_success_guess() -> None:
    """同じ path の metadata/blob があっても別 key の原要求成立を答えない。"""

    db = UploadDatabase()
    await db.upload()
    with pytest.raises(DocumentUploadNotFoundError):
        await _lookup(db, uuid4())
    db.storage.get.assert_not_called()
    db.storage.put.assert_awaited_once()


@pytest.mark.parametrize("field", [
    "size", "storage_key", "original_session_id", "request_checksum",
])
async def test_changed_reservation_during_put_never_publishes_or_compensates(field: str) -> None:
    """元予約と最終 transaction の全 descriptor が違えば、同じ key が存在しても拒否する。"""

    db = UploadDatabase()

    def change_intent() -> None:
        """外部書込後に合成 DB の一つの元事実を変え、原予約比較を検査する。"""

        values = {
            "size": 6, "storage_key": "different-safe-key", "original_session_id": uuid4(),
            "request_checksum": "sha256:" + "0" * 64,
        }
        setattr(db.intents[0], field, values[field])

    db.on_put = change_intent
    with pytest.raises(DocumentUploadInvalidError):
        await db.upload()
    assert not db.documents and db.intents[0].state == "PENDING"
    db.storage.put.assert_awaited_once()
    db.storage.delete.assert_not_awaited()


@pytest.mark.parametrize("failure", ["none", "storage", "commit-kept", "commit-lost", "expiry"])
async def test_delete_keeps_receipt_and_charge_until_exact_cleanup_protocol_exists(
    failure: str,
) -> None:
    """目録/byte 削除の成否によらず占用を保持し、清理要求だけを目録と同時に commit する。"""

    db = UploadDatabase()
    document = await db.upload()
    db.document = db.documents[0]
    intent = db.intents[0]
    if failure == "storage":
        db.storage.delete.side_effect = FileStorageError("Synthetic delete result unavailable")
        expected: type[BaseException] = FileStorageError
    elif failure.startswith("commit-"):
        db.commit_error, db.commit_persists = True, failure == "commit-kept"
        expected = ConnectionError
    elif failure == "expiry":
        db.on_flush = lambda: expire(db)
        expected = UnauthorizedSessionError
    if failure == "none":
        await db.remove_document()
    else:
        with pytest.raises(expected):
            await db.remove_document()
    committed = failure in {"none", "storage", "commit-kept"}
    assert bool(db.documents) is not committed
    assert (intent.cleanup_requested_at is not None) is committed
    assert intent.state == "PUBLISHED" and intent.size == document.size
    if intent.cleanup_requested_at is not None:
        assert intent.published_at is not None
        assert intent.cleanup_requested_at >= intent.published_at
    if failure in {"commit-kept", "commit-lost", "expiry"}:
        db.storage.delete.assert_not_awaited()
    else:
        db.storage.delete.assert_awaited_once_with(intent.storage_key)
    if failure == "expiry":
        db.auth_session.idle_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    _restart(db)
    assert await _usage(db) == document.size
    assert (await _lookup(db)).document == document
    if committed:
        with pytest.raises(DocumentNotFoundError):
            await db.document_service.get_document(
                project_id=db.project.id, document_id=document.document_id,
            )
        assert await db.upload(upload_key=intent.upload_key) == document
        assert not db.documents and len(db.intents) == 1
        db.storage.put.assert_awaited_once()


async def test_deleted_document_old_receipt_does_not_alias_same_name_reupload() -> None:
    """新鍵の同名 upload は新 ID と追加占用になり、旧公開回执は元 ID のまま残る。"""

    db = UploadDatabase()
    original = await db.upload()
    original_key = db.last_upload_key
    db.document = db.documents[0]
    await db.remove_document()
    replacement = await db.upload()
    assert replacement.document_id != original.document_id
    assert (await _lookup(db, original_key)).document == original
    assert await _usage(db) == original.size + replacement.size
    assert len(db.documents) == 1 and len(db.intents) == 2
    assert db.storage.put.await_count == 2


@pytest.mark.parametrize("value", [None, "", "not-a-uuid", UUID(int=0), 1, True])
@pytest.mark.parametrize("operation", ["upload", "lookup"])
async def test_upload_key_is_required_non_nil_uuid_before_transactions(
    value: object, operation: str,
) -> None:
    """欠落/文字列/bool を暗黙 UUID に補完せず、原キーを HTTP 外からも必須にする。"""

    db = UploadDatabase()
    with pytest.raises((TypeError, ValueError)):
        if operation == "upload":
            await db.document_service.upload_document(
                project_id=db.project.id, upload_key=value, access=db.access,  # type: ignore[arg-type]
                folder="", name="note.txt", data=b"hello", content_type="text/plain",
            )
        else:
            await db.document_service.get_upload(
                project_id=db.project.id, upload_key=value, access=db.access,  # type: ignore[arg-type]
            )
    assert db.transactions == 0 and not db.intents
    db.storage.put.assert_not_awaited()


async def test_upload_missing_key_cannot_silently_generate_an_unconfirmable_intent() -> None:
    """service 入口も原 key を要求し、server が別鍵を自動生成して確認不能にしない。"""

    db = UploadDatabase()
    with pytest.raises(TypeError):
        await db.document_service.upload_document(  # type: ignore[call-arg]
            project_id=db.project.id, access=db.access, folder="", name="note.txt", data=b"hello",
            content_type="text/plain",
        )
    assert not db.intents and db.transactions == 0
    db.storage.put.assert_not_awaited()
