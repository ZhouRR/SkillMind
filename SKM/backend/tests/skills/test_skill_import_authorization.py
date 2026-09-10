"""同期 import の原会話・短 transaction・外部 PUT と保存の分離を実用例で検証する。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import ORMExecuteState, Session, make_transient_to_detached

from skillmind.auth.domain import generate_session_credentials
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.db.models import AuthSession, User
from skillmind.skills.domain import InlineSkillFile, SkillStorageUnavailableError, UploadSkillFile
from skillmind.skills.importer import SkillImportError
from skillmind.storage import FileStorageError
from skillmind.storage.blob import StoredBlob
from skillmind.users.domain import UserAccess, UserAdministrationDeniedError
from tests.skills.skill_import_authorization_harness import (
    INLINE_FILES,
    KINDS,
    UPLOAD_FILES,
    ImportKind,
    ImportSession,
)
from tests.skills.test_skill_publication_authorization import EXPIRY, NOW, Clock


async def prepare(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    *,
    reused: bool = False,
) -> ImportSession:
    """本番 parser の保存済み資産を使い、復用時にも fake 許可結果を追加しない。"""
    Clock.current = NOW
    monkeypatch.setattr("skillmind.skills.service.datetime", Clock)
    session = ImportSession()
    session.auth_session.idle_expires_at = EXPIRY
    session.auth_session.absolute_expires_at = EXPIRY
    if reused:
        await session.operate(kind)
        session.reset_observations()
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("reused", [False, True])
async def test_original_admin_is_locked_and_reuse_preserves_import_audit(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    reused: bool,
) -> None:
    """新規/復用の両方に原 ADMIN を要求し、復用は原 URI/導入者/時刻を変えない。"""
    session = await prepare(monkeypatch, kind, reused=reused)
    before = session.frozen_values()
    result = await session.operate(kind)
    assert result.organization_id == session.user.organization_id
    assert len(session.sources) == len(session.interpretations) == 1
    source = session.sources[0]
    assert source.imported_by == session.user.id
    assert source.id == result.skill_source_id
    assert session.interpretations[0].skill_source_id == source.id
    assert session.interpretations[0].origin == "deterministic_parser"
    assert session.timeline[:3] == ["organization:1", "select-User:1", "select-AuthSession:1"]
    assert not session.transaction_active and session.rollbacks == 0
    assert session.storage.deletions == []
    if reused:
        assert session.frozen_values() == before
        assert session.added == [] and session.storage.attempts == []
        assert session.transactions == session.commits == 1
    elif kind == "inline":
        assert source.storage_uri.startswith("database://")
        assert session.storage.attempts == []
        assert session.timeline[-2:] == ["flush:2", "commit:1"]
    else:
        assert source.storage_uri.startswith("s3://skillmind/organizations/")
        assert session.transactions == session.commits == 6
        assert session.timeline[-2:] == ["flush:7", "commit:6"]
        assert len(session.storage.attempts) == 2
        prefix = source.storage_uri.removeprefix("s3://skillmind/")
        for file in UPLOAD_FILES:
            assert await session.storage.get(f"{prefix}/{file.path}") == file.data


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("reused", [False, True])
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
async def test_import_and_reuse_reject_current_persisted_credential_failures_before_put(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    reused: bool,
    denial: str,
) -> None:
    """呼出側の古い actor や任意 org/imported_by では保存も既存結果の復用も許可しない。"""
    session = await prepare(monkeypatch, kind, reused=reused)
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
        session.organization = None
    before = session.frozen_values()
    with pytest.raises(expected):
        await session.operate(kind)
    assert session.frozen_values() == before and session.storage.attempts == []
    assert session.added == [] and "source:1" not in session.timeline


def stages(kind: ImportKind, reused: bool) -> tuple[str, ...]:
    """二 file の現行短 transaction 数を固定し、未到達の注入点を合格にしない。"""
    transaction_count = 1 if kind == "inline" or reused else 6
    source_count = 2 if kind == "upload" else 1
    flush_count = 1 if reused else (2 if kind == "inline" else 7)
    return (
        *(
            f"{stage}:{index}"
            for index in range(1, transaction_count + 1)
            for stage in ("organization", "select-User", "select-AuthSession")
        ),
        *(f"source:{index}" for index in range(1, source_count + 1)),
        "interpretation:1",
        *(f"flush:{index}" for index in range(1, flush_count + 1)),
        *(
            ("put-start:1", "put-done:1", "put-start:2", "put-done:2")
            if kind == "upload" and not reused
            else ()
        ),
    )


WAIT_CASES = [
    (kind, reused, stage)
    for kind in KINDS
    for reused in (False, True)
    for stage in stages(kind, reused)
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,reused,stage", WAIT_CASES)
@pytest.mark.parametrize("expiry", ["idle", "absolute"])
async def test_every_wait_rechecks_original_expiry_and_never_publishes_partial_source(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    reused: bool,
    stage: str,
    expiry: str,
) -> None:
    """PUT が終わった場合も期限等号で保存を拒否し、既存/部分 blob の消去は推測しない。"""
    session = await prepare(monkeypatch, kind, reused=reused)
    if expiry == "idle":
        session.auth_session.absolute_expires_at = EXPIRY + timedelta(hours=1)
    else:
        session.auth_session.idle_expires_at = EXPIRY + timedelta(hours=1)
    before = session.frozen_values()

    def waited(point: str) -> None:
        """一つの本番 await の完了だけを遅延として表す。"""
        if point == stage:
            Clock.current = EXPIRY

    session.on_step = waited
    with pytest.raises(UnauthorizedSessionError):
        await session.operate(kind)
    assert stage in session.timeline and session.frozen_values() == before
    assert not session.transaction_active and session.storage.deletions == []
    if stage in {"put-start:1", "put-done:1"}:
        assert len(session.storage.attempts) == 1


SQL_CASES = [
    (kind, reused, stage)
    for kind, reused, stage in WAIT_CASES
    if stage.startswith(("source:", "interpretation:", "flush:"))
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,reused,stage", SQL_CASES)
@pytest.mark.parametrize("expired", [False, True])
async def test_failed_sql_uses_private_authorization_snapshot_not_expired_orm(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    reused: bool,
    stage: str,
    expired: bool,
) -> None:
    """原資格 ORM が expire 済みでも、新しい SQL を発行せず失敗分類だけを行う。"""
    session = await prepare(monkeypatch, kind, reused=reused)
    before = session.frozen_values()
    failure = OperationalError("Synthetic import SQL failure", {}, RuntimeError("no connection"))
    for row in (session.user, session.auth_session):
        make_transient_to_detached(row)
    with Session() as database:
        database.add_all([session.user, session.auth_session])
        attempted: list[str] = []

        def reject_sql(state: ORMExecuteState) -> None:
            """失敗 snapshot の代わりに元の ORM が読まれたら即時失敗させる。"""
            attempted.append(str(state.statement))
            raise AssertionError("Failure classification must not load ORM")

        def fail(point: str) -> None:
            """SQL エラーの時点で User/Session だけを expire させる。"""
            if point == stage:
                database.expire_all()
                if expired:
                    Clock.current = EXPIRY
                raise failure

        session.on_step = fail
        event.listen(database, "do_orm_execute", reject_sql)
        try:
            with pytest.raises(UnauthorizedSessionError if expired else OperationalError) as result:
                await session.operate(kind)
            if not expired:
                assert result.value is failure
            assert attempted == [] and inspect(session.user).expired_attributes
            assert session.frozen_values() == before
            assert session.storage.deletions == [] and not session.transaction_active
        finally:
            event.remove(database, "do_orm_execute", reject_sql)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["put-start:1", "put-done:1", "put-start:2", "put-done:2"])
@pytest.mark.parametrize("failure_kind", ["storage", "cancel"])
async def test_partial_put_failure_or_cancel_never_retries_deletes_or_saves_source(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    failure_kind: str,
) -> None:
    """最初の bytes が残っていても一括成功を返さず、取消は遠端停止とみなさない。"""
    session = await prepare(monkeypatch, "upload")
    before = session.frozen_values()
    failure = FileStorageError("Synthetic private provider detail")
    cancellation = asyncio.CancelledError()

    def fail(point: str) -> None:
        """保存前/保存後の観測点を分け、同じ応答エラーの二結末を表す。"""
        if point == stage:
            if failure_kind == "cancel":
                raise cancellation
            raise failure

    session.on_step = fail
    expected = asyncio.CancelledError if failure_kind == "cancel" else SkillStorageUnavailableError
    with pytest.raises(expected) as result:
        await session.operate("upload")
    if failure_kind == "cancel":
        assert result.value is cancellation
    else:
        assert str(result.value) == "Skill source storage is unavailable"
    assert len(session.storage.attempts) == int(stage[-1])
    assert session.frozen_values() == before and session.storage.deletions == []
    assert not session.transaction_active


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["key", "size", "boolean-size", "content_type", "sha256", "object-type"]
)
async def test_wrong_put_receipt_cannot_publish_even_when_original_bytes_exist(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """Provider の受付記録は key/長さ/型/実 hash を一致させ、bool を長さとして受けない。"""
    session = await prepare(monkeypatch, "upload")
    if field == "key":
        session.storage.transform = lambda receipt: replace(receipt, key="other/key")
    elif field == "size":
        session.storage.transform = lambda receipt: replace(receipt, size=receipt.size + 1)
    elif field == "boolean-size":
        session.storage.transform = lambda receipt: replace(receipt, size=True)
    elif field == "content_type":
        session.storage.transform = lambda receipt: replace(receipt, content_type="text/plain")
    elif field == "sha256":
        session.storage.transform = lambda receipt: replace(receipt, sha256="sha256:" + "0" * 64)
    else:
        # 壊れた Provider が型契約に反して None を返す信頼境界だけを合成する。
        session.storage.transform = lambda receipt: cast(StoredBlob, None)
    with pytest.raises(
        SkillStorageUnavailableError, match=r"^Skill source storage is unavailable$"
    ):
        await session.operate("upload")
    assert session.sources == [] and session.interpretations == []
    assert len(session.storage.attempts) == 1 and session.storage.deletions == []
    key, data, _ = session.storage.attempts[0]
    assert await session.storage.get(key) == data


@pytest.mark.asyncio
async def test_reused_inline_source_upload_keeps_database_uri_without_any_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じ原 bytes の upload は既存 database source を修復/移動せず、blob 未配線でも復用する。"""
    session = await prepare(monkeypatch, "inline", reused=True)
    before = session.frozen_values()
    stored = await session.service(storage_configured=False).save_upload(
        access=session.access,
        files=UPLOAD_FILES,
    )
    assert stored.skill_source_id == session.sources[0].id
    assert session.sources[0].storage_uri.startswith("database://")
    assert session.frozen_values() == before and session.storage.attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_mutated_caller_input_and_replaced_access_cannot_change_frozen_import(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
) -> None:
    """最初の await 後に元 list を置換しても、資格/bytes/保存 hash を取り違えない。"""
    session = await prepare(monkeypatch, kind)
    original = session.access
    inline = list(INLINE_FILES)
    upload = list(UPLOAD_FILES)
    expected = session.service().preview_inline(inline).normalized_package["source"]["content_hash"]

    def changed(point: str) -> None:
        """外部の要求変数だけを更新し、本物の現在 User/Session は変更しない。"""
        if point == "organization:1":
            inline[:] = [InlineSkillFile(path="SKILL.md", content="# Different\n")]
            upload[:] = [
                UploadSkillFile(path="SKILL.md", data=b"# Different\n", content_type="text/plain")
            ]
            session.access = replace(original, actor=replace(original.actor, user_id=uuid4()))

    session.on_step = changed
    if kind == "inline":
        stored = await session.service().save_inline(access=original, files=inline)
    else:
        stored = await session.service().save_upload(access=original, files=upload)
    assert stored.source_hash == expected
    assert session.sources[0].imported_by == original.actor.user_id
    assert session.sources[0].source_snapshot_json == [
        {"path": file.path, "content": file.content} for file in INLINE_FILES
    ]
    if kind == "upload":
        assert [(data, content_type) for _, data, content_type in session.storage.attempts] == [
            (file.data, file.content_type) for file in UPLOAD_FILES
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["revoked", "role", "disabled"])
async def test_first_put_completed_then_revocation_prevents_second_put_and_source_publication(
    monkeypatch: pytest.MonkeyPatch,
    denial: str,
) -> None:
    """最初の blob が保存されても、原会話が失効した後の次の外部書込を開始しない。"""
    session = await prepare(monkeypatch, "upload")

    def changed(point: str) -> None:
        """PUT 後の短資格 transaction は更新後の保存行を読む。"""
        if point == "put-done:1":
            if denial == "revoked":
                session.auth_session.revoked_at = NOW
            elif denial == "role":
                session.user.system_role = "USER"
            else:
                session.user.status = "DISABLED"

    session.on_step = changed
    with pytest.raises(UnauthorizedSessionError):
        await session.operate("upload")
    assert len(session.storage.attempts) == 1
    key, data, _ = session.storage.attempts[0]
    assert await session.storage.get(key) == data
    assert session.sources == [] and session.interpretations == []
    assert session.storage.deletions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["storage", "receipt"])
async def test_provider_failure_rechecks_expired_original_session_before_static_storage_error(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    """PUT の結果が壊れても、失効した原資格に後着の storage 状態を公開しない。"""
    session = await prepare(monkeypatch, "upload")
    if failure_kind == "receipt":
        session.storage.transform = lambda receipt: replace(receipt, key="wrong")

    def expire(point: str) -> None:
        """実 bytes の保存後、失敗の分類に入る前に原期限へ到達させる。"""
        if point == "put-done:1":
            Clock.current = EXPIRY
            if failure_kind == "storage":
                raise FileStorageError("Synthetic private detail")

    session.on_step = expire
    with pytest.raises(UnauthorizedSessionError):
        await session.operate("upload")
    assert session.sources == [] and len(session.storage.attempts) == 1
    assert session.storage.deletions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,reused,stage", SQL_CASES)
async def test_sql_wait_cancellation_restores_only_current_transaction_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    reused: bool,
    stage: str,
) -> None:
    """取消を認証拒否へ変換せず、既に完了した短 transaction と外部 bytes は区別する。"""
    session = await prepare(monkeypatch, kind, reused=reused)
    before = session.frozen_values()
    cancellation = asyncio.CancelledError()

    def cancel(point: str) -> None:
        """指定した DB 待機だけで取消を発生させる。"""
        if point == stage:
            raise cancellation

    session.on_step = cancel
    with pytest.raises(asyncio.CancelledError) as result:
        await session.operate(kind)
    assert result.value is cancellation
    assert stage in session.timeline and session.frozen_values() == before
    assert session.storage.deletions == [] and not session.transaction_active


COMMIT_CASES = [
    (kind, index) for kind in KINDS for index in range(1, (1 if kind == "inline" else 6) + 1)
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,index", COMMIT_CASES)
@pytest.mark.parametrize("outcome", ["committed", "not-committed"])
@pytest.mark.parametrize("error_kind", ["connection", "sqlalchemy", "operational"])
async def test_any_short_transaction_commit_unknown_is_not_reclassified_or_retried(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    index: int,
    outcome: str,
    error_kind: str,
) -> None:
    """無変更の短 commit を含め、応答喪失後に次 PUT や保存を自動で再開しない。"""
    session = await prepare(monkeypatch, kind)
    before = session.frozen_values()
    if error_kind == "sqlalchemy":
        session.commit_error = SQLAlchemyError("Synthetic import commit unknown")
    elif error_kind == "operational":
        session.commit_error = OperationalError("Synthetic commit unknown", {}, RuntimeError())
    for row in (session.user, session.auth_session):
        make_transient_to_detached(row)
    with Session() as database:
        database.add_all([session.user, session.auth_session])

        def commit_wait(point: str) -> None:
            """指定 commit だけで期限と元 ORM を失効させ、二つの物理結末を合成する。"""
            if point == f"commit:{index}":
                session.commit_outcome = outcome
                Clock.current = EXPIRY
                database.expire_all()

        session.on_step = commit_wait
        with pytest.raises(type(session.commit_error)) as result:
            await session.operate(kind)
        assert result.value is session.commit_error
        assert session.transactions == index
        assert session.commits == index - int(outcome == "not-committed")
        final = kind == "inline" or index == 6
        assert (session.frozen_values() != before) is (final and outcome == "committed")
        assert session.storage.deletions == [] and not session.transaction_active


@pytest.mark.asyncio
async def test_all_storage_keys_are_validated_before_first_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """parser が許す長い後半 path でも、storage key 不正なら前半 file を先に保存しない。"""
    session = await prepare(monkeypatch, "upload")
    files = (
        UPLOAD_FILES[0],
        UploadSkillFile(
            path="references/" + "x" * 129 + ".md",
            data=b"# Long path\n",
            content_type="text/markdown",
        ),
    )
    with pytest.raises(SkillImportError) as result:
        await session.service().save_upload(access=session.access, files=files)
    assert result.value.code == "invalid_file_path"
    assert session.storage.attempts == [] and session.sources == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "path",
    [
        "C:/SKILL.md",
        "C:SKILL.md",
        "//server/share/SKILL.md",
        "../SKILL.md",
        "./SKILL.md",
        "references//rules.md",
        "references/./rules.md",
        "references/a\x00.md",
    ],
)
def test_preview_rejects_drive_unc_dot_empty_and_control_paths(kind: ImportKind, path: str) -> None:
    """公開 preview を通じて検証し、source の歴史 hash 計算を変更しない。"""
    session = ImportSession()
    with pytest.raises(SkillImportError) as result:
        if kind == "inline":
            session.service().preview_inline(
                (INLINE_FILES[0], InlineSkillFile(path=path, content="# Invalid\n"))
            )
        else:
            session.service().preview_upload(
                (
                    UPLOAD_FILES[0],
                    UploadSkillFile(path=path, data=b"# Invalid\n", content_type="text/markdown"),
                )
            )
    assert result.value.code == "invalid_file_path"
    assert session.transactions == 0 and session.storage.attempts == []


def test_upload_preview_keeps_unicode_directory_root_and_relative_backslashes() -> None:
    """合法な browser directory root と相対区切りの互換性を残す。"""
    service = ImportSession().service()
    original = service.preview_upload(UPLOAD_FILES)
    wrapped = service.preview_upload(
        tuple(replace(file, path="資料\\" + file.path.replace("/", "\\")) for file in UPLOAD_FILES)
    )
    assert wrapped.normalized_package == original.normalized_package
    assert wrapped.runtime_manifest_draft == original.runtime_manifest_draft


def new_admin_access(session: ImportSession, *, different_admin: bool) -> UserAccess:
    """実 User/会話行と v2 credential を追加し、既存の会話を復活させない。"""
    actor = session.access.actor
    if different_admin:
        user = User(
            id=uuid4(),
            organization_id=session.organization_id,
            email="other-import-admin@example.test",
            display_name="Other synthetic admin",
            system_role="ADMIN",
            status="ACTIVE",
        )
        session.users.append(user)
        actor = replace(actor, user_id=user.id, email=user.email, display_name=user.display_name)
    credentials = generate_session_credentials()
    session.auth_sessions.append(
        AuthSession(
            id=uuid4(),
            user_id=actor.user_id,
            token_hash=credentials.session_token_hash,
            csrf_token_hash=credentials.csrf_token_hash,
            credential_version=2,
            system_role_at_login="ADMIN",
            revoked_at=None,
            created_at=NOW,
            last_seen_at=NOW,
            idle_expires_at=EXPIRY,
            absolute_expires_at=EXPIRY,
        )
    )
    return UserAccess(
        actor=actor,
        request_id=uuid4(),
        session_token=credentials.session_token,
        csrf_token=credentials.csrf_token,
    )


@pytest.mark.asyncio
async def test_new_session_cannot_take_over_original_upload_after_first_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じ User の有効な新会話があっても、進行中の原 PUT はその資格へ乗り換えない。"""
    session = await prepare(monkeypatch, "upload")

    def relogin(point: str) -> None:
        """原 token を取り消し、新 token は独立した保存行として追加する。"""
        if point == "put-done:1":
            session.auth_session.revoked_at = NOW
            session.access = new_admin_access(session, different_admin=False)

    session.on_step = relogin
    with pytest.raises(UnauthorizedSessionError):
        await session.operate("upload")
    assert len(session.auth_sessions) == 2 and session.auth_sessions[-1].revoked_at is None
    assert len(session.storage.attempts) == 1 and session.sources == []
    assert session.storage.deletions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_other_current_admin_reuses_without_rewriting_original_importer_or_uri(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
) -> None:
    """同組織の新しい ADMIN 会話で読めても、元の導入監査を現在の actor に置換しない。"""
    session = await prepare(monkeypatch, kind, reused=True)
    before = session.frozen_values()
    original_importer = session.sources[0].imported_by
    session.access = new_admin_access(session, different_admin=True)
    stored = await session.operate(kind)
    assert stored.skill_source_id == session.sources[0].id
    assert session.sources[0].imported_by == original_importer != session.access.actor.user_id
    assert session.frozen_values() == before
    assert session.added == [] and session.storage.attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", ["new-hash", "other-organization"])
async def test_source_reuse_requires_both_current_organization_and_original_hash(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    case: str,
) -> None:
    """同名や別組織の同 hash は原 source の復用証明にならない。"""
    session = await prepare(monkeypatch, kind, reused=True)
    original = session.sources[0]
    if case == "other-organization":
        original.organization_id = uuid4()
    before = deepcopy(
        {column.key: getattr(original, column.key) for column in original.__table__.columns}
    )
    inline = INLINE_FILES
    upload = UPLOAD_FILES
    if case == "new-hash":
        inline = (
            replace(INLINE_FILES[0], content=INLINE_FILES[0].content + "Additional rule.\n"),
            INLINE_FILES[1],
        )
        upload = (replace(UPLOAD_FILES[0], data=inline[0].content.encode()), UPLOAD_FILES[1])
    if kind == "inline":
        stored = await session.service().save_inline(access=session.access, files=inline)
    else:
        stored = await session.service().save_upload(access=session.access, files=upload)
    assert stored.skill_source_id != original.id and len(session.sources) == 2
    assert stored.organization_id == session.organization_id
    assert {
        column.key: getattr(original, column.key) for column in original.__table__.columns
    } == before
    assert len(session.interpretations) == 2
    if case == "new-hash":
        assert stored.source_hash != original.content_hash
    else:
        assert stored.source_hash == original.content_hash
    assert len(session.storage.attempts) == (2 if kind == "upload" else 0)
