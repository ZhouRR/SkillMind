"""実 Skill/資格 repository を接続し、凍結と発行の原会話を offline で再検証する。

DB 応答と rollback は合成であり、PostgreSQL の競争や物理 commit 時点を証明しない。
lock 後の資格変更は防御的再検証の注入で、共通 lock を持つ writer の並行更新ではない。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from uuid import uuid4

import pytest
from sqlalchemy import Select, and_, event, inspect
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import ORMExecuteState, Session, make_transient_to_detached

from skillmind.auth.domain import generate_session_credentials
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.db.models import (
    AuthSession,
    Organization,
    SkillInterpretation,
    SkillSource,
    SkillVersion,
    User,
)
from skillmind.skills.design_validation import SkillDesignSource
from skillmind.skills.domain import (
    ManifestGateFinding,
    SkillInterpretationNotFoundError,
    SkillInterpretationNotReadyError,
    SkillPublishGateError,
    SkillVersionNotFoundError,
    SkillVersionTransitionError,
    StoredSkillVersion,
)
from skillmind.skills.manifest_gate import ManifestValidator
from skillmind.users.domain import UserAdministrationDeniedError
from tests.skills.test_skill_draft_validation import DraftSession
from tests.skills.test_skill_publication_validation import (
    Model,
    PublicationResult,
    PublicationSession,
    PublicationTransaction,
)

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
EXPIRY = NOW + timedelta(minutes=1)


class Clock(datetime):
    """実待機をせず、業務 service が取得する時刻だけを制御する。"""

    current = NOW

    @classmethod
    def now(cls, tz: object = None) -> Self:
        """期限の直前/同時刻を wall clock に依存せず返す。"""
        return cls.combine(cls.current.date(), cls.current.timetz())


class ControlledTransaction(PublicationTransaction):
    """資産変更の合成 rollback と、commit 応答喪失の二つの可能性を分ける。"""

    def __init__(self, session: AuthorizationSession) -> None:
        """原資格の外部失効は rollback 対象に含めない。"""
        super().__init__(session)
        self.owner = session
        self.original: list[tuple[Model, dict[str, Any]]] = []

    async def __aenter__(self) -> Self:
        """原 aggregate の保存列だけを snapshot に取り、SQL 接続を作らない。"""
        await super().__aenter__()
        self.owner.transactions += 1
        self.original = [
            (
                row,
                deepcopy(
                    {column.key: getattr(row, column.key) for column in row.__table__.columns}
                ),
            )
            for row in self.owner.rows
        ]
        self.owner.original_rows = self.owner.rows
        return self

    def restore(self) -> None:
        """自分の資産変更だけを戻し、独立した User/AuthSession 失効を復活させない。"""
        self.owner.failure_values = self.owner.frozen_values()
        for row, values in self.original:
            for key, value in values.items():
                setattr(row, key, deepcopy(value))
        self.owner.rows = self.owner.original_rows
        self.owner.rollbacks += 1

    async def __aexit__(self, kind: object, error: object, traceback: object) -> None:
        """成功 DTO を返す前に transaction の終了が必要なことを観測する。"""
        if kind is not None:
            self.restore()
            await super().__aexit__(kind, error, traceback)
            return
        self.owner.emit("commit")
        if self.owner.commit_outcome == "not-committed":
            self.restore()
            await super().__aexit__(ConnectionError, self.owner.commit_error, None)
            raise self.owner.commit_error
        self.owner.commits += 1
        await super().__aexit__(None, None, None)
        if self.owner.commit_outcome == "committed":
            raise self.owner.commit_error


class AuthorizationSession(PublicationSession):
    """共有 aggregate/実 SQL に待機観測だけを追加し、認可結果は差し替えない。"""

    def __init__(self) -> None:
        """clock と外部失効注入、資産 rollback の観測を独立して準備する。"""
        super().__init__()
        self.auth_session.idle_expires_at = EXPIRY
        self.auth_session.absolute_expires_at = EXPIRY
        self.timeline: list[str] = []
        self.visits: dict[str, int] = {}
        self.on_step: Callable[[str], None] | None = None
        self.transactions = self.commits = self.rollbacks = 0
        self.original_rows = self.rows
        self.failure_values: dict[str, object] | None = None
        self.commit_outcome: str | None = None
        self.commit_error: Exception = ConnectionError("Synthetic commit outcome unavailable")

    def begin(self) -> ControlledTransaction:
        """既存 Service の transaction を実接続なしで観測する。"""
        return ControlledTransaction(self)

    def emit(self, name: str) -> None:
        """応答が返る境界の回数も固定し、初回/二回目 source lookup を区別する。"""
        self.visits[name] = self.visits.get(name, 0) + 1
        point = f"{name}:{self.visits[name]}"
        self.timeline.append(point)
        if self.on_step:
            self.on_step(point)

    async def get(self, model: object, identity: object, **options: object) -> Model | None:
        """元 PK/lock の SQL seam を維持し、ロード完了時の失効だけを注入する。"""
        result = await super().get(model, identity, **options)
        assert isinstance(model, type)
        self.emit(f"get-{model.__name__}")
        return result

    async def scalar(self, statement: Select[tuple[Any, ...]]) -> Model | None:
        """Organization の本番 WHERE と lock を基底で検証する。"""
        parameters = statement.compile().params
        assert statement.whereclause is not None
        assert statement.whereclause.compare(Organization.id == parameters["id_1"])
        result = await super().scalar(statement)
        self.emit("organization")
        return result

    async def scalars(self, statement: Select[tuple[Any, ...]]) -> PublicationResult:
        """本番 User/Session/Manifest query の結果を作った後で待機を表す。"""
        entity = statement.column_descriptions[0]["entity"]
        parameters = statement.compile().params
        # 文字列中に条件が存在するだけで OR や別 scope の誤りを合格にしない。
        if entity in {User, AuthSession}:
            predicate = (
                and_(
                    User.organization_id == parameters["organization_id_1"],
                    User.id.in_(parameters["id_1"]),
                )
                if entity is User
                else and_(
                    AuthSession.user_id == parameters["user_id_1"],
                    AuthSession.token_hash == parameters["token_hash_1"],
                )
            )
            assert statement.whereclause is not None
            assert statement.whereclause.compare(predicate)
        result = await super().scalars(statement)
        self.emit(f"select-{entity.__name__}")
        return result

    async def flush(self) -> None:
        """SQL flush の成功/失敗と最終期限判定の間に観測点を置く。"""
        await super().flush()
        self.emit("flush")

    async def perform(self) -> StoredSkillVersion:
        """必須の原 UserAccess で実 publish service を呼ぶ。"""
        return await self.service().publish_skill_version(
            access=self.access,
            skill_version_id=self.version.id,
            accepted_warnings=frozenset(),
        )

    def reset_observations(self) -> None:
        """再送の検証前に観測だけを消し、原保存値/資格/受付記録を変更しない。"""
        self.timeline.clear()
        self.visits.clear()
        self.events.clear()
        self.transactions = self.commits = self.rollbacks = 0
        self.failure_values = None


class DraftAuthorizationSession(DraftSession, AuthorizationSession):
    """DRAFT の親 flush/原版検索をそのまま使い、同じ資格観測を合成する。"""

    async def scalars(self, statement: Select[tuple[Any, ...]]) -> PublicationResult:
        """DRAFT 固有 SELECT だけを追加記録し、資格 SELECT は重複させない。"""
        result = await super().scalars(statement)
        entity = statement.column_descriptions[0]["entity"]
        if entity not in {Organization, User, AuthSession} and "runtime_manifests" not in str(
            statement
        ):
            self.emit(f"draft-{entity.__name__}")
        return result

    async def perform(self) -> StoredSkillVersion:
        """原解釈 ID と同じ UserAccess で実 DRAFT 用例を呼ぶ。"""
        return await self.service().create_version_draft(
            access=self.access, interpretation_id=self.interpretation.id
        )


async def prepare(
    monkeypatch: pytest.MonkeyPatch, operation: str, path: str
) -> AuthorizationSession:
    """新規/原受付記録の二経路を、合法な実 validator と同じ固定 clock で準備する。"""
    Clock.current = NOW
    monkeypatch.setattr("skillmind.skills.service.datetime", Clock)
    session: AuthorizationSession = (
        DraftAuthorizationSession() if operation == "draft" else AuthorizationSession()
    )
    evaluate = ManifestValidator.evaluate

    def observed(
        validator: ManifestValidator, source: SkillDesignSource
    ) -> tuple[bool, tuple[ManifestGateFinding, ...]]:
        """本番 gate を実行し終えた時刻境界だけを観測する。"""
        result = evaluate(validator, source)
        session.emit("gate")
        return result

    monkeypatch.setattr(ManifestValidator, "evaluate", observed)
    if path == "replay":
        await session.perform()
        session.reset_observations()
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["publish", "draft"])
@pytest.mark.parametrize("path", ["new", "replay"])
async def test_original_admin_is_locked_before_skill_and_checked_through_flush(
    monkeypatch: pytest.MonkeyPatch, operation: str, path: str
) -> None:
    """初回と原受付記録の両方で共有 Org→User→Session と最後の flush を通す。"""
    session = await prepare(monkeypatch, operation, path)
    result = await session.perform()
    assert result.skill_version_id == session.version.id
    assert session.timeline[:3] == ["organization:1", "select-User:1", "select-AuthSession:1"]
    assert session.timeline[-2:] == [f"flush:{session.visits['flush']}", "commit:1"]
    assert session.transactions == session.commits == 1
    assert session.rollbacks == 0
    if operation == "publish":
        version_load = next(load for load in session.loads if load[0] is SkillVersion)
        assert version_load[2] == {"with_for_update": True, "populate_existing": True}
        assert result.published_by == session.user.id
    if path == "replay":
        assert session.visits.get("gate", 0) == (1 if operation == "draft" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["publish", "draft"])
@pytest.mark.parametrize("path", ["new", "replay"])
@pytest.mark.parametrize(
    "denial",
    [
        "missing-org",
        "missing-user",
        "foreign-user",
        "missing-session",
        "foreign-session",
        "wrong-token",
        "revoked",
        "disabled",
        "legacy",
        "wrong-csrf-hash",
        "role-changed",
        "current-user",
        "csrf",
        "malformed-token",
    ],
)
async def test_current_persisted_credential_refusals_never_change_skill_assets(
    monkeypatch: pytest.MonkeyPatch, operation: str, path: str, denial: str
) -> None:
    """古い ADMIN actor を提示しても現在の原会話判定を迂回できない。"""
    session = await prepare(monkeypatch, operation, path)
    expected: type[Exception] = UnauthorizedSessionError
    if denial == "missing-org":
        session.organization = None
    elif denial == "missing-user":
        session.users.clear()
    elif denial == "foreign-user":
        session.user.organization_id = uuid4()
    elif denial == "missing-session":
        session.auth_sessions.clear()
    elif denial == "foreign-session":
        session.auth_session.user_id = uuid4()
    elif denial == "wrong-token":
        session.auth_session.token_hash = "sha256:" + "0" * 64
    elif denial == "revoked":
        session.auth_session.revoked_at = NOW
    elif denial == "disabled":
        session.user.status = "DISABLED"
    elif denial == "legacy":
        session.auth_session.credential_version = 1
    elif denial == "wrong-csrf-hash":
        session.auth_session.csrf_token_hash = "sha256:" + "0" * 64
    elif denial == "role-changed":
        session.user.system_role = "USER"
    elif denial == "current-user":
        session.user.system_role = session.auth_session.system_role_at_login = "USER"
        expected = UserAdministrationDeniedError
    elif denial == "csrf":
        session.access = replace(session.access, csrf_token="")
        expected = CsrfRejectedError
    else:
        session.access = replace(session.access, session_token="invalid-synthetic-session")
    before = session.frozen_values()
    with pytest.raises(expected):
        await session.perform()
    assert session.frozen_values() == before
    assert session.commits == 0
    assert not any(point.startswith("get-") for point in session.timeline)
    assert session.visits.get("gate", 0) == 0
    if denial == "malformed-token":
        assert session.transactions == 0


PUBLISH_STAGES = [
    (path, stage)
    for path in ("new", "replay")
    for stage in (
        "organization:1",
        "select-User:1",
        "select-AuthSession:1",
        "get-SkillVersion:1",
        "get-SkillSource:1",
        "get-Skill:1",
        "select-RuntimeManifest:1",
        "flush:1",
        *(("get-SkillSource:2", "get-SkillInterpretation:1", "gate:1") if path == "new" else ()),
    )
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,stage", PUBLISH_STAGES)
@pytest.mark.parametrize("expiry_kind", ["idle", "absolute"])
async def test_publication_expiry_at_each_wait_and_actual_gate_never_commits(
    monkeypatch: pytest.MonkeyPatch, path: str, stage: str, expiry_kind: str
) -> None:
    """元期限の同時刻を含め、資源待機/実 gate/flush 後に古い now を再利用しない。"""
    session = await prepare(monkeypatch, "publish", path)
    if expiry_kind == "idle":
        session.auth_session.absolute_expires_at = EXPIRY + timedelta(hours=1)
    else:
        session.auth_session.idle_expires_at = EXPIRY + timedelta(hours=1)
    before = session.frozen_values()

    def waited(point: str) -> None:
        """保存期限は固定したまま、新しい clock だけを境界まで進める。"""
        if point == stage:
            Clock.current = EXPIRY

    session.on_step = waited
    with pytest.raises(UnauthorizedSessionError):
        await session.perform()
    assert stage in session.timeline
    assert session.frozen_values() == before
    assert session.commits == 0 and session.rollbacks == 1
    if path == "new" and stage != "flush:1":
        assert session.failure_values == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,stage",
    [
        ("new", "get-SkillInterpretation:1"),
        ("new", "get-SkillSource:1"),
        ("new", "gate:1"),
        ("new", "flush:1"),
        ("new", "flush:2"),
        ("new", "flush:3"),
        ("replay", "get-SkillInterpretation:1"),
        ("replay", "get-SkillSource:1"),
        ("replay", "draft-SkillVersion:1"),
        ("replay", "flush:1"),
    ],
)
async def test_draft_parent_flushes_and_replay_keep_the_original_credential_gate(
    monkeypatch: pytest.MonkeyPatch, path: str, stage: str
) -> None:
    """新しい親行 flush や既存版 lookup の待機で失効しても DRAFT を返さない。"""
    session = await prepare(monkeypatch, "draft", path)
    before = session.frozen_values()

    def waited(point: str) -> None:
        """初期判定後の clock を進め、source/親 FK 待機を飛ばした検証を防ぐ。"""
        if point == stage:
            Clock.current = EXPIRY

    session.on_step = waited
    with pytest.raises(UnauthorizedSessionError):
        await session.perform()
    assert stage in session.timeline
    assert session.frozen_values() == before
    assert session.commits == 0 and session.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["publish", "draft"])
@pytest.mark.parametrize("path", ["new", "replay"])
async def test_one_microsecond_before_expiry_remains_authorized(
    monkeypatch: pytest.MonkeyPatch, operation: str, path: str
) -> None:
    """期限条件を過剰に広げず、最終判定点でまだ有効な会話は受理する。"""
    session = await prepare(monkeypatch, operation, path)
    Clock.current = EXPIRY - timedelta(microseconds=1)
    await session.perform()
    assert session.commits == 1 and session.rollbacks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("different_admin", [False, True])
async def test_new_valid_admin_session_replays_original_publisher_and_historical_gate(
    monkeypatch: pytest.MonkeyPatch,
    different_admin: bool,
) -> None:
    """新規 ADMIN/会話の資格だけを再確認し、旧公開版の内容や発行監査を更新しない。"""
    session = await prepare(monkeypatch, "publish", "replay")
    original_publisher = session.version.published_by
    original_time = session.version.published_at
    session.version.gate_report_json = {"passed": False, "findings": []}
    session.manifest.manifest_json["capability_blueprint"] = None
    session.missing.add(SkillInterpretation)
    previous_user, previous_session = session.user, session.auth_session
    previous_session.revoked_at = NOW
    if different_admin:
        session.user = User(
            id=uuid4(),
            organization_id=previous_user.organization_id,
            email="second-admin@example.test",
            display_name="Second synthetic admin",
            password_hash="unused",
            system_role="ADMIN",
            status="ACTIVE",
            row_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.users.append(session.user)
    credentials = generate_session_credentials()
    session.auth_session = AuthSession(
        id=uuid4(),
        user_id=session.user.id,
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
    session.auth_sessions.append(session.auth_session)
    session.access = replace(
        session.access,
        actor=replace(session.access.actor, user_id=session.user.id),
        session_token=credentials.session_token,
        csrf_token=credentials.csrf_token,
    )
    before = session.frozen_values()
    result = await session.perform()
    assert result.published_by == original_publisher == previous_user.id
    assert (result.published_by != session.user.id) is different_admin
    assert result.published_at == original_time
    assert session.frozen_values() == before
    assert session.visits.get("gate", 0) == 0
    assert previous_session.revoked_at == NOW
    assert session.auth_session.id != previous_session.id


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["revoked", "role-roundtrip", "status-roundtrip"])
async def test_restored_account_never_revives_revoked_original_session(
    monkeypatch: pytest.MonkeyPatch, denial: str
) -> None:
    """役割/状態が元に戻っても、管理 transaction が失効させた会話は再生しない。"""
    session = await prepare(monkeypatch, "publish", "replay")
    session.auth_session.revoked_at = NOW
    if denial == "role-roundtrip":
        session.user.system_role = "USER"
        session.user.system_role = "ADMIN"
    elif denial == "status-roundtrip":
        session.user.status = "DISABLED"
        session.user.status = "ACTIVE"
    before = session.frozen_values()
    with pytest.raises(UnauthorizedSessionError):
        await session.perform()
    assert session.auth_session.revoked_at == NOW
    assert session.frozen_values() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["not-found", "gate", "transition", "draft-not-ready", "draft-not-found"]
)
@pytest.mark.parametrize("expired", [False, True])
async def test_domain_failure_rechecks_time_without_changing_its_original_classification(
    monkeypatch: pytest.MonkeyPatch, failure: str, expired: bool
) -> None:
    """まだ有効なら原 404/409、待機で期限切れならその資源情報を先に返さない。"""
    operation = "draft" if failure.startswith("draft-") else "publish"
    session = await prepare(monkeypatch, operation, "new")
    if failure == "not-found":
        session.missing.add(SkillSource)
        expected: type[Exception] = SkillVersionNotFoundError
    elif failure == "gate":
        session.version.gate_report_json = {"passed": False, "findings": []}
        expected = SkillPublishGateError
    elif failure == "transition":
        session.version.status = "DEPRECATED"
        expected = SkillVersionTransitionError
    elif failure == "draft-not-ready":
        session.interpretation.status = "FAILED"
        expected = SkillInterpretationNotReadyError
    else:
        session.missing.add(SkillInterpretation)
        expected = SkillInterpretationNotFoundError
    before = session.frozen_values()

    def waited(point: str) -> None:
        """資格 lock は成功し、その後に本来の領域拒否となる lookup で期限を迎える。"""
        if expired and point.startswith("get-"):
            Clock.current = EXPIRY

    session.on_step = waited
    with pytest.raises(UnauthorizedSessionError if expired else expected):
        await session.perform()
    assert session.frozen_values() == before
    assert session.commits == 0


FLUSH_BOUNDARIES = [
    ("publish", "new", 1),
    ("publish", "replay", 1),
    ("draft", "new", 1),
    ("draft", "new", 2),
    ("draft", "new", 3),
    ("draft", "replay", 1),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,path,flush_index", FLUSH_BOUNDARIES)
@pytest.mark.parametrize("expired", [False, True])
async def test_sql_failure_uses_private_credential_snapshot_without_lazy_loading(
    monkeypatch: pytest.MonkeyPatch, operation: str, path: str, flush_index: int, expired: bool
) -> None:
    """failed flush で元 ORM が expire しても、新時刻の失敗分類は SQL を実行しない。"""
    session = await prepare(monkeypatch, operation, path)
    before = session.frozen_values()
    failure = OperationalError("Synthetic flush failure", {}, RuntimeError("not connected"))
    make_transient_to_detached(session.user)
    make_transient_to_detached(session.auth_session)
    with Session() as database:
        database.add_all([session.user, session.auth_session])
        attempted_sql: list[str] = []

        def reject_sql(state: ORMExecuteState) -> None:
            """bind の無い Session でも lazy SELECT が試みられたらその場で失敗する。"""
            attempted_sql.append(str(state.statement))
            raise AssertionError("Failure authorization must not load expired ORM")

        event.listen(database, "do_orm_execute", reject_sql)

        def failed_flush(point: str) -> None:
            """現在資格の原値を失効させ、flush の例外後に生 ORM へ触れないことを確かめる。"""
            if point == f"flush:{flush_index}":
                database.expire_all()
                if expired:
                    Clock.current = EXPIRY
                raise failure

        session.on_step = failed_flush
        try:
            with pytest.raises(UnauthorizedSessionError if expired else OperationalError) as result:
                await session.perform()
            if not expired:
                assert result.value is failure
            assert attempted_sql == []
            assert inspect(session.user).expired_attributes
            assert inspect(session.auth_session).expired_attributes
            assert session.frozen_values() == before
            assert session.transactions == session.rollbacks == 1 and session.commits == 0
        finally:
            event.remove(database, "do_orm_execute", reject_sql)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,path,flush_index", FLUSH_BOUNDARIES)
async def test_cancelled_flush_propagates_without_retry_or_partial_publication(
    monkeypatch: pytest.MonkeyPatch, operation: str, path: str, flush_index: int
) -> None:
    """取消を新しい transaction/retry や成功受付記録に変換しない。"""
    session = await prepare(monkeypatch, operation, path)
    before = session.frozen_values()
    cancelled = asyncio.CancelledError()

    def cancel(point: str) -> None:
        """この合成場面では commit 前の指定した flush が取消される。"""
        if point == f"flush:{flush_index}":
            raise cancelled

    session.on_step = cancel
    with pytest.raises(asyncio.CancelledError) as result:
        await session.perform()
    assert result.value is cancelled
    assert session.transactions == session.rollbacks == 1 and session.commits == 0
    assert session.frozen_values() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["publish", "draft"])
@pytest.mark.parametrize("outcome", ["committed", "not-committed"])
@pytest.mark.parametrize("error_kind", ["connection", "sqlalchemy", "operational"])
async def test_commit_response_unknown_is_not_retried_or_declared_success(
    monkeypatch: pytest.MonkeyPatch, operation: str, outcome: str, error_kind: str
) -> None:
    """commit 応答喪失後の期限/ORM 失効で、原例外を認証拒否に再分類しない。"""
    session = await prepare(monkeypatch, operation, "new")
    before = session.frozen_values()
    session.commit_outcome = outcome
    if error_kind == "sqlalchemy":
        session.commit_error = SQLAlchemyError("Synthetic commit outcome unavailable")
    elif error_kind == "operational":
        session.commit_error = OperationalError(
            "Synthetic commit outcome unavailable", {}, RuntimeError("not connected")
        )
    make_transient_to_detached(session.user)
    make_transient_to_detached(session.auth_session)
    with Session() as database:
        database.add_all([session.user, session.auth_session])
        attempted_sql: list[str] = []

        def reject_sql(state: ORMExecuteState) -> None:
            """物理 commit 後か不明な原 ORM の lazy load が起きたら失敗させる。"""
            attempted_sql.append(str(state.statement))
            raise AssertionError("Commit outcome classification must not load ORM")

        def commit_wait(point: str) -> None:
            """最終業務認証を通った後だけ clock と ORM を失効させる。"""
            if point == "commit:1":
                Clock.current = EXPIRY
                database.expire_all()

        session.on_step = commit_wait
        event.listen(database, "do_orm_execute", reject_sql)
        try:
            with pytest.raises(type(session.commit_error)) as result:
                await session.perform()
            assert result.value is session.commit_error
            assert attempted_sql == []
            assert inspect(session.user).expired_attributes
            assert inspect(session.auth_session).expired_attributes
            assert Clock.current == EXPIRY
            assert session.transactions == 1
            assert session.commits == (1 if outcome == "committed" else 0)
            assert (session.frozen_values() == before) is (outcome == "not-committed")
        finally:
            event.remove(database, "do_orm_execute", reject_sql)
