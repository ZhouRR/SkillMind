"""評価の原要求、共有資格と有界 SQL page を局部 transaction seam で検証する。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, make_transient_to_detached

from skillmind.auth.domain import generate_session_credentials
from skillmind.auth.sessions import UnauthorizedSessionError
from skillmind.db.models import (
    AuthSession,
    Evaluation,
    Organization,
    Project,
    ProjectMember,
    Run,
    RunResult,
    User,
)
from skillmind.evaluations.domain import (
    EvaluationIntegrityError,
    EvaluationResultMismatchError,
    EvaluationResultNotFoundError,
    EvaluationRevisionProposal,
    EvaluationSubmissionConflictError,
    EvaluationSubmissionNotFoundError,
    InvalidEvaluationCommandError,
    InvalidEvaluationCursorError,
    InvalidEvaluationRevisionError,
    evaluation_request_hash,
    resolve_json_pointer,
    strict_evaluation_json,
)
from skillmind.runs.domain import RunNotFoundError
from tests.evaluations.submission_harness import EvaluationDatabase
from tests.runs.test_creation_authorization import invalidate


class EvaluationClock:
    """原期限を変更せず、待機後の新時刻だけを進める。"""

    current = datetime.now(UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        """service の明示時計だけを合成値に置き換える。"""

        return cls.current.astimezone(tz)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> type[EvaluationClock]:
    """期限越えを sleep や保存値の書換えで模擬しない。"""

    EvaluationClock.current = datetime.now(UTC)
    monkeypatch.setattr("skillmind.evaluations.service.datetime", EvaluationClock)
    return EvaluationClock


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), -float("inf"), "\ud800", {1: "coerced"}, (1,), {1}, object()],
)
def test_canonical_request_rejects_non_json_and_invalid_utf8(value: Any) -> None:
    """json.dumps が暗黙変換できる key/tuple も受理しない。"""

    with pytest.raises(InvalidEvaluationCommandError):
        strict_evaluation_json(value)


def test_canonical_json_rejects_cycles_and_distinguishes_booleans_numbers() -> None:
    """Python の True==1 を原要求の同一性に使わない。"""

    cycle: list[Any] = []
    cycle.append(cycle)
    with pytest.raises(InvalidEvaluationCommandError):
        strict_evaluation_json(cycle)
    assert strict_evaluation_json(True) != strict_evaluation_json(1)
    assert strict_evaluation_json(1) != strict_evaluation_json(1.0)


@pytest.mark.parametrize("pointer", ["/\u00b2", "/\u0661", "/" + "9" * 5000])
def test_pointer_rejects_non_ascii_or_huge_array_index_without_raw_conversion_error(
    pointer: str,
) -> None:
    """Unicode digit や Python int 制限を通じて生の例外を返さない。"""

    with pytest.raises(InvalidEvaluationRevisionError):
        resolve_json_pointer(["original"], pointer)


@pytest.mark.asyncio
async def test_first_replay_confirmation_and_legacy_append_preserve_original_result() -> None:
    """新キーは一行、旧キーなしは毎回別行。いずれも Result を変更しない。"""

    db = EvaluationDatabase()
    before = deepcopy(db.result.data_json)
    first = await db.submit()
    replay = await db.submit()
    confirmed = await db.get_submission()
    assert not first.idempotent_replay and replay.idempotent_replay
    assert replay == confirmed and first.evaluation == confirmed.evaluation
    assert len(db.evaluations) == 1 and db.result.data_json == before
    old = await db.create()
    other = await db.create()
    assert old.evaluation_id != other.evaluation_id
    assert all(
        row.submission_key is None and row.request_hash is None for row in db.evaluations[1:]
    )


@pytest.mark.asyncio
async def test_lock_order_uses_shared_credential_and_fk_compatible_share_locks() -> None:
    """新書込の SQL は Org→User→Session→Project/member→Run→Result→Evaluation だけを固定する。"""

    db = EvaluationDatabase()
    await db.submit()
    statements = [query for query in db.statements if query._for_update_arg is not None]
    entities = [Organization, User, AuthSession, Project, ProjectMember, Run, RunResult, Evaluation]
    assert [query.column_descriptions[0]["entity"] for query in statements] == entities
    dialect_factory: Callable[..., Dialect] = postgresql.dialect
    for query, entity in zip(statements, entities, strict=True):
        sql = str(query.compile(dialect=dialect_factory()))
        assert sql.endswith(
            "FOR UPDATE" if entity in {Organization, AuthSession, Evaluation} else "FOR SHARE"
        )
        if entity is not Organization:
            assert query.get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["rating", "verdict", "comment", "suggestion", "reason", "order"])
async def test_same_key_changed_canonical_content_never_appends(field: str) -> None:
    """各意味 field と有序 revision の相違を上書きや成功重放へ変換しない。"""

    db = EvaluationDatabase()
    db.command = replace(
        db.command,
        revisions=(
            db.command.revisions[0],
            EvaluationRevisionProposal("/issue/id", "new", "source"),
        ),
    )
    await db.submit()
    original = db.command
    if field == "rating":
        db.command = replace(original, rating=1)
    elif field == "verdict":
        db.command = replace(original, verdict=type(original.verdict).INACCURATE)
    elif field == "comment":
        db.command = replace(original, comment="different")
    elif field == "order":
        db.command = replace(original, revisions=tuple(reversed(original.revisions)))
    else:
        revision = replace(
            original.revisions[0],
            **({"suggested_value": True} if field == "suggestion" else {"reason": "other"}),
        )
        db.command = replace(original, revisions=(revision, original.revisions[1]))
    with pytest.raises(EvaluationSubmissionConflictError):
        await db.submit()
    assert len(db.evaluations) == 1 and not db.staged and db.rollbacks == 1


@pytest.mark.asyncio
async def test_canonical_object_order_replays_but_boolean_is_not_number() -> None:
    """共有 JSON 正規化だけを原要求比較に使い、dict== の numeric 同値を持ち込まない。"""

    db = EvaluationDatabase()
    revision = replace(db.command.revisions[0], suggested_value={"a": 1, "b": None})
    db.command = replace(db.command, revisions=(revision,))
    await db.submit()
    db.command = replace(
        db.command, revisions=(replace(revision, suggested_value={"b": None, "a": 1}),)
    )
    assert (await db.submit()).idempotent_replay
    db.command = replace(
        db.command, revisions=(replace(revision, suggested_value={"a": True, "b": None}),)
    )
    with pytest.raises(EvaluationSubmissionConflictError):
        await db.submit()


@pytest.mark.asyncio
async def test_request_and_credential_are_copied_before_first_await() -> None:
    """呼出元の可変 DTO を lock 待機中に書換えても保存内容・actor は変化しない。"""

    db = EvaluationDatabase()
    original = {"nested": ["first"]}
    db.command = replace(
        db.command, revisions=(replace(db.command.revisions[0], suggested_value=original),)
    )
    expected_hash = evaluation_request_hash(
        db.command,
        result_id=db.result.id,
        submission_key=db.submission_key,
    )
    access = db.access

    def mutate(step: str) -> None:
        if step == "lock:Organization":
            original["nested"].append("late")
            db.access = replace(access, actor=replace(access.actor, user_id=uuid4()))

    db.on_step = mutate
    # add seam の所有者判定も元要求を維持し、fixture 自身の mutable reference を利用しない。
    db.session.add.side_effect = db.staged.append
    stored = await db.submit()
    assert stored.evaluation.revisions[0].suggested_value == {"nested": ["first"]}
    assert db.evaluations[0].request_hash == expected_hash
    assert stored.evaluation.user_id == access.actor.user_id


@pytest.mark.asyncio
async def test_stored_and_returned_nested_json_never_alias_each_other() -> None:
    """DTO を表示側が編集しても ORM 原記録や Result の内容を書き換えない。"""

    db = EvaluationDatabase()
    db.result.data_json["fields"][0]["value"] = {"nested": [None]}
    db.command = replace(
        db.command,
        revisions=(
            replace(
                db.command.revisions[0],
                suggested_value={"nested": [True]},
            ),
        ),
    )
    response = await db.submit()
    response.evaluation.revisions[0].original_value["nested"].append("display")
    response.evaluation.revisions[0].suggested_value["nested"].append("display")
    assert db.evaluations[0].revision_json[0]["original_value"] == {"nested": [None]}
    assert db.evaluations[0].revision_json[0]["suggested_value"] == {"nested": [True]}
    assert (await db.get_submission()).evaluation.revisions[0].original_value == {"nested": [None]}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["submit", "create", "get_submission", "list_page", "legacy-list"])
@pytest.mark.parametrize(
    "failure",
    [
        "revoked",
        "disabled",
        "role",
        "csrf",
        "missing-session",
        "missing-user",
        "missing-org",
        "member",
        "cross-org",
        "archived",
    ],
)
async def test_all_entry_paths_reuse_current_credential_and_membership(
    path: str, failure: str
) -> None:
    """旧履歴・新確認も actor stub を信用せず、読取だけは CSRF/帰档によって妨げない。"""

    db = EvaluationDatabase()
    await db.submit()
    error = invalidate(db, failure)
    db.events.clear()

    async def call() -> Any:
        if path == "legacy-list":
            return await db.evaluation_service.list_for_run(
                project_id=db.project.id,
                run_id=db.run.id,
                access=db.access,
            )
        return await getattr(db, path)()

    if path in {"get_submission", "list_page", "legacy-list"} and failure in {"csrf", "archived"}:
        await call()
    else:
        with pytest.raises(error):
            await call()
        assert "result" not in db.events and "lookup" not in db.events
    assert len(db.evaluations) == 1


@pytest.mark.asyncio
async def test_new_valid_session_can_confirm_but_other_actor_cannot() -> None:
    """原要求は user に帰属し、失効した古い session の credential を新読取へ持ち込まない。"""

    db = EvaluationDatabase()
    first = await db.submit()
    credentials = generate_session_credentials()
    db.auth_session.revoked_at = datetime.now(UTC)
    replacement = deepcopy(db.auth_session)
    replacement.id, replacement.revoked_at = uuid4(), None
    replacement.token_hash = credentials.session_token_hash
    replacement.csrf_token_hash = credentials.csrf_token_hash
    db.auth_sessions.append(replacement)
    db.access = replace(
        db.access, session_token=credentials.session_token, csrf_token=credentials.csrf_token
    )
    assert (await db.get_submission()).evaluation == first.evaluation
    db.user.id = replacement.user_id = uuid4()
    assert db.member is not None
    db.member.user_id = db.user.id
    db.access = replace(db.access, actor=replace(db.access.actor, user_id=db.user.id))
    with pytest.raises(EvaluationSubmissionNotFoundError):
        await db.get_submission()
    assert len(db.evaluations) == 1


@pytest.mark.asyncio
async def test_forged_command_actor_is_rejected_before_database() -> None:
    """内部 caller に残る user_id を他人へ差し替えても資格を生成できない。"""

    db = EvaluationDatabase()
    db.command = replace(db.command, user_id=uuid4())
    with pytest.raises(UnauthorizedSessionError):
        await db.submit()
    assert db.transactions == 0 and not db.evaluations


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["submit", "create", "get_submission", "list_page"])
@pytest.mark.parametrize("step", ["lock:Project", "lock:Run", "result", "flush"])
async def test_waiting_past_original_expiry_blocks_every_normal_exit(
    clock: type[EvaluationClock],
    path: str,
    step: str,
) -> None:
    """原期限は固定し、flush/重放/履歴の出口を新しい now で拒否する。"""

    db = EvaluationDatabase()
    await db.submit()
    initial = clock.current
    db.auth_session.idle_expires_at = initial + timedelta(seconds=1)

    def advance(name: str) -> None:
        if name == step:
            clock.current = initial + timedelta(seconds=2)

    db.on_step = advance
    with pytest.raises(UnauthorizedSessionError):
        await getattr(db, path)()
    assert len(db.evaluations) == 1 and not db.staged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["missing-run", "missing-result", "mismatch", "miss", "conflict", "cursor"]
)
async def test_domain_rejection_is_not_returned_after_original_session_expires(
    clock: type[EvaluationClock],
    failure: str,
) -> None:
    """不存在/衝突/游標拒否も期限切れ actor に保存状態を漏らさない。"""

    db = EvaluationDatabase()
    await db.submit()
    initial = clock.current
    db.auth_session.idle_expires_at = initial + timedelta(seconds=1)
    db.run_present = failure != "missing-run"
    db.result_present = failure != "missing-result"
    if failure == "miss":
        db.submission_key = uuid4()
    if failure == "conflict":
        db.command = replace(db.command, comment="conflict")

    def advance(step: str) -> None:
        if step == "lock:Run":
            clock.current = initial + timedelta(seconds=2)

    db.on_step = advance
    with pytest.raises(UnauthorizedSessionError):
        if failure == "mismatch":
            await db.evaluation_service.submit(
                db.command,
                submission_key=db.submission_key,
                result_id=uuid4(),
                access=db.access,
            )
        elif failure == "cursor":
            await db.list_page(after=uuid4())
        else:
            await (db.get_submission() if failure == "miss" else db.submit())
    assert len(db.evaluations) == 1


@pytest.mark.asyncio
async def test_failed_flush_rechecks_snapshot_without_expired_orm_io(
    clock: type[EvaluationClock],
) -> None:
    """実 ORM expire と failed flush の後も新 now で原資格を検証し、暗黙 SQL を呼ばない。"""

    db = EvaluationDatabase()
    initial = clock.current
    db.auth_session.idle_expires_at = initial + timedelta(seconds=1)
    with Session() as expired_session:
        for row in (db.user, db.auth_session):
            make_transient_to_detached(row)
            expired_session.add(row)

        def fail(step: str) -> None:
            if step == "flush":
                clock.current = initial + timedelta(seconds=2)
                expired_session.expire_all()
                raise SQLAlchemyError("synthetic failed flush")

        db.on_step = fail
        with pytest.raises(UnauthorizedSessionError):
            await db.submit()
    assert not db.evaluations and db.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_commit_unknown_never_returns_success_or_retries(persisted: bool) -> None:
    """flush と commit 応答を分離し、後続原 GET だけが保存の有無を確認する。"""

    db = EvaluationDatabase()
    db.commit_unknown = persisted
    with pytest.raises(ConnectionError):
        await db.submit()
    assert len(db.evaluations) == int(persisted) and db.transactions == 1
    db.commit_unknown = None
    if persisted:
        assert (await db.get_submission()).idempotent_replay
    else:
        with pytest.raises(EvaluationSubmissionNotFoundError):
            await db.get_submission()
    assert len(db.evaluations) == int(persisted)


@pytest.mark.asyncio
async def test_commit_wait_and_cancellation_never_become_business_error() -> None:
    """commit 待機中は DTO を返さず、取消を拒否結果や別 key 再試行に変換しない。"""

    db = EvaluationDatabase()
    db.commit_release = asyncio.Event()
    task = asyncio.create_task(db.submit())
    try:
        await asyncio.wait_for(db.commit_entered.wait(), timeout=2)
        assert not task.done() and not db.evaluations and db.staged
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        db.commit_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert db.rollbacks == 1 and not db.evaluations and not db.staged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "half-hash",
        "key",
        "hash",
        "rating-bool",
        "verdict",
        "comment",
        "missing-original",
        "missing-suggested",
        "extra",
        "original",
        "suggested",
        "pointer",
        "reason",
        "timestamp",
    ],
)
@pytest.mark.parametrize("path", ["submit", "get_submission", "list_page"])
async def test_corrupt_records_never_become_valid_receipts_or_history(
    damage: str, path: str
) -> None:
    """保存損傷を default/coerce で隠さず、原記録を修復や追記しない。"""

    db = EvaluationDatabase()
    await db.submit()
    row = db.evaluations[0]
    if damage == "half-hash":
        row.request_hash = None
    elif damage == "key":
        row.submission_key = db.submission_key = uuid4()
    elif damage == "hash":
        row.request_hash = "sha256:" + "0" * 64
    elif damage == "rating-bool":
        row.rating = True
    elif damage == "verdict":
        row.verdict = "invented"
    elif damage == "comment":
        row.comment = "mutated"
    elif damage.startswith("missing-"):
        del row.revision_json[0][damage.removeprefix("missing-") + "_value"]
    elif damage == "extra":
        row.revision_json[0]["private"] = "not public"
    elif damage == "timestamp":
        row.created_at = datetime.now()
    else:
        key = damage + "_value" if damage in {"original", "suggested"} else damage
        row.revision_json[0][key] = 1
    with pytest.raises(EvaluationIntegrityError):
        await getattr(db, path)()
    assert len(db.evaluations) == 1 and not db.staged


@pytest.mark.asyncio
@pytest.mark.parametrize("damaged_key", [None, UUID(int=0)])
async def test_page_rejects_nil_or_half_request_identity(damaged_key: UUID | None) -> None:
    """原 key 検索の範囲外になった identity 損傷も、履歴の全行投影では隠さない。"""

    db = EvaluationDatabase()
    await db.submit()
    db.evaluations[0].submission_key = damaged_key
    with pytest.raises(EvaluationIntegrityError):
        await db.list_page()
    assert len(db.evaluations) == 1


@pytest.mark.asyncio
async def test_original_null_is_valid_but_boolean_and_number_originals_differ() -> None:
    """原値 null を missing と混同せず、原 hash 対象外の original_value も原 Result と検査する。"""

    db = EvaluationDatabase()
    db.result.data_json["fields"][0]["value"] = None
    assert (await db.submit()).evaluation.revisions[0].original_value is None
    db.result.data_json["fields"][0]["value"] = True
    db.evaluations[0].revision_json[0]["original_value"] = 1
    with pytest.raises(EvaluationIntegrityError):
        await db.get_submission()


@pytest.mark.asyncio
async def test_cursor_sql_is_bounded_total_order_and_keeps_every_tied_timestamp() -> None:
    """SQL limit+1 と UUID tie-break により、同時刻の行を跨頁で落とさない。"""

    db = EvaluationDatabase()
    first = await db.submit()
    template = db.evaluations[0]
    for index in range(1, 102):
        row = deepcopy(template)
        row.id, row.submission_key, row.request_hash = UUID(int=index), None, None
        db.evaluations.append(row)
    expected = sorted(db.evaluations, key=lambda item: (item.created_at, item.id))
    page = await db.list_page(limit=100)
    assert [item.evaluation_id for item in page.items] == [item.id for item in expected[:100]]
    assert page.next_cursor == expected[99].id
    next_page = await db.list_page(limit=100, after=page.next_cursor)
    assert [item.evaluation_id for item in next_page.items] == [item.id for item in expected[100:]]
    assert next_page.next_cursor is None and len(next_page.items) == 2
    assert first.evaluation.evaluation_id in {item.id for item in expected}
    pages = [query for query in db.statements if query._limit_clause is not None]
    assert len(pages) == 2 and all(query._limit_clause.value == 101 for query in pages)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["nil", "unknown", "foreign", "damaged-anchor"])
async def test_invalid_cursor_cannot_lend_another_result_position(cursor: str) -> None:
    """anchor の scope と投影を確定してから page SQL を発行する。"""

    db = EvaluationDatabase()
    receipt = await db.submit()
    after = receipt.evaluation.evaluation_id
    if cursor == "nil":
        after = UUID(int=0)
    elif cursor == "unknown":
        after = uuid4()
    elif cursor == "foreign":
        db.evaluations[0].result_id = uuid4()
    else:
        db.evaluations[0].request_hash = None
    db.events.clear()
    with pytest.raises(
        EvaluationIntegrityError if cursor == "damaged-anchor" else InvalidEvaluationCursorError
    ):
        await db.list_page(after=after)
    assert "page" not in db.events


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True, -1])
async def test_page_invalid_limit_is_not_unbounded(limit: int) -> None:
    """内部 caller の limit も型と範囲を検証し、全件 SELECT へ fallback しない。"""

    db = EvaluationDatabase()
    with pytest.raises(InvalidEvaluationCommandError):
        await db.list_page(limit=limit)
    assert "page" not in db.events


@pytest.mark.asyncio
async def test_result_and_project_scope_errors_do_not_create_rows() -> None:
    """Result 未完成・別 Result・別 Project を区別して拒否し、現在値へ勝手に付替えない。"""

    db = EvaluationDatabase()
    with pytest.raises(EvaluationResultMismatchError):
        await db.evaluation_service.submit(
            db.command,
            submission_key=db.submission_key,
            result_id=uuid4(),
            access=db.access,
        )
    db.result_present = False
    with pytest.raises(EvaluationResultNotFoundError):
        await db.submit()
    db.run.project_id = uuid4()
    with pytest.raises(RunNotFoundError):
        await db.submit()
    assert not db.evaluations
