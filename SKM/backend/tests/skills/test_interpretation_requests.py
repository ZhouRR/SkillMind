"""非同期解釈の一度限りの開始と原会話失効を、実 service と局部 SQL で検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.db.models import OutboxMessage
from skillmind.skills.domain import SaveModelInterpretationCommand, SkillInterpretationStatus
from skillmind.skills.interpretation_requests import (
    InterpretationRequestConflictError,
    InterpretationRequestDeniedError,
    InterpretationRequestNotFoundError,
)
from tests.skills.interpretation_request_harness import RequestSession

KEY = "sha256:" + "b" * 64
INPUT = {"format_version": 1, "model": "synthetic-model", "request": {"source": "synthetic"}}


async def accept(session: RequestSession):
    """実 parser で導入した source を、本番台帳へ受理する。"""

    source = await session.operate("inline")
    return await session.ledger().accept(
        access=session.access,
        request_id=uuid4(),
        skill_source_id=source.skill_source_id,
        execution_key=KEY,
        frozen_input=INPUT,
    )


def command(request) -> SaveModelInterpretationCommand:
    """台帳の採用 transaction を検証する合成失敗を作り、Blueprint を補造しない。"""

    return SaveModelInterpretationCommand(
        organization_id=request.organization_id,
        skill_source_id=request.skill_source_id,
        execution_key=request.execution_key,
        interpreter_version="synthetic/v1",
        model=INPUT["model"],
        status=SkillInterpretationStatus.FAILED,
        compatibility_level="unsupported",
        confidence=0.0,
        summary="Synthetic failure",
        assumptions=(),
        questions=(),
        diagnostics=(),
        normalized_package={},
        manifest_draft={},
        report=None,
        execution={"execution_key": request.execution_key, "error_code": "provider_error"},
    )


@pytest.mark.asyncio
async def test_accept_replay_preserves_original_session_input_and_only_one_outbox() -> None:
    """同 ID/入力の再送は同じ原要求を返し、資格や Outbox を更新しない。"""

    session = RequestSession()
    request = await accept(session)
    frozen = session.frozen_values()
    payload = deepcopy(INPUT)
    repeated = await session.ledger().accept(
        access=replace(session.access, request_id=uuid4()),
        request_id=request.request_id,
        skill_source_id=request.skill_source_id,
        execution_key=KEY,
        frozen_input=payload,
    )
    payload["request"]["source"] = "changed"
    assert repeated == request
    assert session.frozen_values() == frozen
    outbox = [row for row in session.rows if isinstance(row, OutboxMessage)]
    assert len(outbox) == 1 and outbox[0].payload_json == {"request_id": str(request.request_id)}
    assert session.requests[0].auth_session_id == session.auth_session.id
    assert session.requests[0].accepted_http_request_id == session.access.request_id
    assert session.access.session_token not in repr(request)
    assert "csrf" not in str(outbox[0].payload_json)


@pytest.mark.asyncio
async def test_same_id_with_changed_input_or_session_is_rejected() -> None:
    """新しい会話や微小な model 変更で原要求を再利用しない。"""

    session = RequestSession()
    request = await accept(session)
    for changed in ({**INPUT, "model": "another-model"}, INPUT):
        if changed is INPUT:
            session.auth_session.id = uuid4()
        with pytest.raises(InterpretationRequestConflictError):
            await session.ledger().accept(
                access=session.access,
                request_id=request.request_id,
                skill_source_id=request.skill_source_id,
                execution_key=KEY,
                frozen_input=changed,
            )
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_new_request_id_cannot_restart_an_unknown_execution() -> None:
    """新 ID を生成しても同じ内容の原処理を横取りせず、再認領もできない。"""

    session = RequestSession()
    request = await accept(session)
    owner = await session.ledger().claim(request.request_id)
    assert owner is not None
    await session.ledger().mark_unknown(owner)
    with pytest.raises(InterpretationRequestConflictError):
        await session.ledger().accept(
            access=session.access,
            request_id=uuid4(),
            skill_source_id=request.skill_source_id,
            execution_key=KEY,
            frozen_input=INPUT,
        )
    assert len(session.requests) == 1 and session.requests[0].status == "UNKNOWN"
    assert await session.ledger().claim(request.request_id) is None


@pytest.mark.asyncio
async def test_foreign_request_is_not_found_for_confirm_or_accept() -> None:
    """組織外の既存 ID は成功 DTO や競合詳細を公開しない。"""

    session = RequestSession()
    request = await accept(session)
    session.requests[0].organization_id = uuid4()
    with pytest.raises(InterpretationRequestNotFoundError):
        await session.ledger().confirm(access=session.access, request_id=request.request_id)
    with pytest.raises(InterpretationRequestNotFoundError):
        await session.ledger().accept(
            access=session.access,
            request_id=request.request_id,
            skill_source_id=request.skill_source_id,
            execution_key=KEY,
            frozen_input=INPUT,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["get-SkillSource", "request-lock", "flush"])
async def test_accept_expiry_after_wait_rolls_back_request_and_outbox(stage: str) -> None:
    """受理時の途中失効は、片側だけの要求や dispatch を残さない。"""

    session = RequestSession()
    source = await session.operate("inline")
    session.reset_observations()
    session.on_step = lambda point: (
        setattr(session.auth_session, "idle_expires_at", datetime.now(UTC))
        if point.startswith(stage + ":")
        else None
    )
    with pytest.raises(UnauthorizedSessionError):
        await session.ledger().accept(
            access=session.access,
            request_id=uuid4(),
            skill_source_id=source.skill_source_id,
            execution_key=KEY,
            frozen_input=INPUT,
        )
    assert not session.requests
    assert not any(isinstance(row, OutboxMessage) for row in session.rows)


@pytest.mark.asyncio
async def test_accept_requires_original_csrf_and_confirm_never_writes_or_claims() -> None:
    """確認は current ADMIN の read で、CSRF なしの再受理にはしない。"""

    session = RequestSession()
    request = await accept(session)
    access = replace(session.access, csrf_token="")
    before = session.frozen_values()
    assert await session.ledger().confirm(access=access, request_id=request.request_id) == request
    assert session.frozen_values() == before
    with pytest.raises(CsrfRejectedError):
        await session.ledger().accept(
            access=access,
            request_id=request.request_id,
            skill_source_id=request.skill_source_id,
            execution_key=KEY,
            frozen_input=INPUT,
        )


@pytest.mark.asyncio
async def test_claim_and_each_call_can_only_be_granted_once_to_original_worker() -> None:
    """Queue 再送、呼出し再読、他 token、return 未観測の修復を全て閉じる。"""

    session = RequestSession()
    request = await accept(session)
    session.reset_observations()
    ledger = session.ledger()
    owner = await ledger.claim(request.request_id)
    assert owner is not None and owner.token not in repr(owner)
    assert await ledger.claim(request.request_id) is None
    assert (
        await ledger.start_call(replace(owner, token="another-worker"), ordinal=0, feedback=None)
        is None
    )
    first = await ledger.start_call(owner, ordinal=0, feedback=None)
    assert first is not None
    assert await ledger.start_call(owner, ordinal=0, feedback=None) is None
    assert await ledger.start_call(owner, ordinal=1, feedback="schema:required") is None
    await ledger.record_return(first)
    second = await ledger.start_call(owner, ordinal=1, feedback="schema:required")
    assert second is not None
    assert await ledger.start_call(owner, ordinal=1, feedback="schema:required") is None
    assert len(session.calls) == 2
    names = [point.split(":")[0] for point in session.timeline]
    assert names[:5] == [
        "request-read",
        "organization",
        "reference-user",
        "reference-session",
        "request-lock",
    ]
    assert "FOR SHARE" in next(query for query in session.queries if "FROM users" in query)
    assert not any("FROM projects" in query for query in session.queries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revocation",
    ["revoked", "idle", "absolute", "disabled", "role", "legacy", "login-role", "missing-session"],
)
async def test_revocation_before_initial_call_is_durable_and_cannot_be_replaced(
    revocation: str,
) -> None:
    """原会話の全失効条件は開始前に拒否し、再配送にも資格を渡さない。"""

    session = RequestSession()
    request = await accept(session)
    owner = await session.ledger().claim(request.request_id)
    assert owner is not None
    if revocation == "revoked":
        session.auth_session.revoked_at = datetime.now(UTC)
    elif revocation == "idle":
        session.auth_session.idle_expires_at = datetime.now(UTC)
    elif revocation == "absolute":
        session.auth_session.absolute_expires_at = datetime.now(UTC)
    elif revocation == "disabled":
        session.user.status = "DISABLED"
    elif revocation == "role":
        session.user.system_role = "USER"
    elif revocation == "legacy":
        session.auth_session.credential_version = 1
    elif revocation == "login-role":
        session.auth_session.system_role_at_login = "USER"
    else:
        session.auth_sessions.clear()
    assert await session.ledger().start_call(owner, ordinal=0, feedback=None) is None
    assert session.requests[0].status == "REVOKED"
    assert session.requests[0].error_code == "authorization_revoked"
    assert not session.calls
    assert await session.ledger().claim(request.request_id) is None


@pytest.mark.asyncio
async def test_revocation_keeps_return_but_denies_repair_and_result() -> None:
    """呼出しの return と成果採用を分け、撤権しても return の事実は保持する。"""

    session = RequestSession()
    request = await accept(session)
    ledger = session.ledger()
    owner = await ledger.claim(request.request_id)
    assert owner is not None
    permit = await ledger.start_call(owner, ordinal=0, feedback=None)
    assert permit is not None
    session.auth_session.revoked_at = datetime.now(UTC)
    await ledger.record_return(permit)
    assert session.calls[0].returned_at is not None
    assert await ledger.start_call(owner, ordinal=1, feedback="schema:required") is None
    before = len(session.interpretations)
    with pytest.raises(InterpretationRequestDeniedError):
        await ledger.finish(owner, command(request))
    assert len(session.interpretations) == before


@pytest.mark.asyncio
async def test_result_flush_expiry_rolls_back_candidate_and_commits_revocation() -> None:
    """結果を flush した後でも、期限切れ候補を採用せず要求の撤権だけを残す。"""

    session = RequestSession()
    request = await accept(session)
    owner = await session.ledger().claim(request.request_id)
    assert owner is not None
    permit = await session.ledger().start_call(owner, ordinal=0, feedback=None)
    assert permit is not None
    await session.ledger().record_return(permit)
    before = len(session.interpretations)
    session.on_step = lambda point: (
        setattr(session.auth_session, "idle_expires_at", datetime.now(UTC))
        if point.startswith("flush:")
        else None
    )
    with pytest.raises(InterpretationRequestDeniedError):
        await session.ledger().finish(owner, command(request))
    assert len(session.interpretations) == before
    assert session.requests[0].status == "REVOKED" and session.requests[0].interpretation_id is None
    assert session.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["committed", "not-committed"])
async def test_unknown_claim_commit_never_returns_a_start_owner(outcome: str) -> None:
    """commit 応答喪失は成功 token を返さず、実 commit の有無を区別して確認する。"""

    session = RequestSession()
    request = await accept(session)
    session.commit_outcome = outcome
    with pytest.raises(ConnectionError):
        await session.ledger().claim(request.request_id)
    assert session.requests[0].status == ("RUNNING" if outcome == "committed" else "QUEUED")
    assert not session.calls
    session.commit_outcome = None
    if outcome == "committed":
        assert await session.ledger().claim(request.request_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["request-lock", "calls", "flush"])
async def test_expiry_during_start_grant_does_not_return_a_permit(stage: str) -> None:
    """認可後の待機で失効した許可は呼出元へ渡さず、commit には撤権を残す。"""

    session = RequestSession()
    request = await accept(session)
    owner = await session.ledger().claim(request.request_id)
    assert owner is not None
    session.on_step = lambda point: (
        setattr(session.auth_session, "idle_expires_at", datetime.now(UTC))
        if point.startswith(stage + ":")
        else None
    )
    assert await session.ledger().start_call(owner, ordinal=0, feedback=None) is None
    assert session.requests[0].status == "REVOKED"
    assert all(call.returned_at is None for call in session.calls)


@pytest.mark.asyncio
async def test_permit_preserves_input_when_caller_changes_owner_while_waiting() -> None:
    """許可に持ち出す入力は呼出元の可変 dict と切り離して固定する。"""

    session = RequestSession()
    request = await accept(session)
    owner = await session.ledger().claim(request.request_id)
    assert owner is not None
    session.on_step = lambda point: (
        owner.request.input.update(model="changed") if point.startswith("calls:") else None
    )
    permit = await session.ledger().start_call(owner, ordinal=0, feedback=None)
    assert permit is not None
    assert permit.owner.request.input["model"] == INPUT["model"]
    assert session.requests[0].input_json["model"] == INPUT["model"]
    assert owner.request.input["model"] == "changed"


@pytest.mark.asyncio
async def test_unknown_call_commit_does_not_reissue_the_same_permit() -> None:
    """開始許可の commit 応答が失われても、同じ ordinal を再取得できない。"""

    session = RequestSession()
    request = await accept(session)
    owner = await session.ledger().claim(request.request_id)
    assert owner is not None
    session.commit_outcome = "committed"
    with pytest.raises(ConnectionError):
        await session.ledger().start_call(owner, ordinal=0, feedback=None)
    session.commit_outcome = None
    assert len(session.calls) == 1 and session.calls[0].returned_at is None
    assert await session.ledger().start_call(owner, ordinal=0, feedback=None) is None
    assert await session.ledger().start_call(owner, ordinal=1, feedback="schema:required") is None


@pytest.mark.asyncio
async def test_result_requires_an_observed_return_and_unchanged_binding() -> None:
    """呼出しなし、return 不明、別モデルの成果を新しい確定結果として保存しない。"""

    session = RequestSession()
    request = await accept(session)
    ledger = session.ledger()
    owner = await ledger.claim(request.request_id)
    assert owner is not None
    with pytest.raises(InterpretationRequestDeniedError):
        await ledger.finish(owner, command(request))
    permit = await ledger.start_call(owner, ordinal=0, feedback=None)
    assert permit is not None
    with pytest.raises(InterpretationRequestDeniedError):
        await ledger.finish(owner, command(request))
    await ledger.record_return(permit)
    with pytest.raises(InterpretationRequestDeniedError):
        await ledger.finish(owner, replace(command(request), model="another-model"))
    stored = await ledger.finish(owner, command(request))
    assert session.requests[0].interpretation_id == stored.interpretation_id


@pytest.mark.asyncio
async def test_unknown_stops_new_calls_but_allows_original_owner_to_record_late_result() -> None:
    """UNKNOWN は停止証明や再開始資格ではなく、元 owner の遅延成果だけを照合できる。"""

    session = RequestSession()
    request = await accept(session)
    ledger = session.ledger()
    owner = await ledger.claim(request.request_id)
    assert owner is not None
    permit = await ledger.start_call(owner, ordinal=0, feedback=None)
    assert permit is not None
    await ledger.mark_unknown(owner)
    assert session.requests[0].status == "UNKNOWN"
    assert await ledger.claim(request.request_id) is None
    assert await ledger.start_call(owner, ordinal=0, feedback=None) is None
    await ledger.record_return(permit)
    stored = await ledger.finish(owner, command(request))
    assert session.requests[0].status == "FAILED"
    assert session.requests[0].interpretation_id == stored.interpretation_id
    assert len(session.calls) == 1 and session.calls[0].returned_at is not None


@pytest.mark.asyncio
async def test_unknown_request_and_corrupted_input_cannot_acquire_start_right() -> None:
    """旧 Queue の UUID と改変された凍結入力に owner を生成しない。"""

    session = RequestSession()
    with pytest.raises(InterpretationRequestNotFoundError):
        await session.ledger().claim(uuid4())
    request = await accept(session)
    session.requests[0].input_json["model"] = "changed"
    assert await session.ledger().claim(request.request_id) is None
    assert session.requests[0].status == "FAILED"
    assert session.requests[0].error_code == "input_integrity"


@pytest.mark.asyncio
async def test_reference_checks_do_not_extend_idle_or_absolute_expiry() -> None:
    """Worker の認領/初回/return/修復は、ブラウザー会話を延命しない。"""

    session = RequestSession()
    request = await accept(session)
    now = datetime.now(UTC)
    session.auth_session.idle_expires_at = now + timedelta(minutes=3)
    session.auth_session.absolute_expires_at = now + timedelta(hours=1)
    original = (
        session.auth_session.idle_expires_at,
        session.auth_session.absolute_expires_at,
        session.auth_session.last_seen_at,
    )
    owner = await session.ledger().claim(request.request_id)
    assert owner is not None
    permit = await session.ledger().start_call(owner, ordinal=0, feedback=None)
    assert permit is not None
    await session.ledger().record_return(permit)
    assert (
        await session.ledger().start_call(owner, ordinal=1, feedback="schema:required") is not None
    )
    assert original == (
        session.auth_session.idle_expires_at,
        session.auth_session.absolute_expires_at,
        session.auth_session.last_seen_at,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_recovery_preserves_call_uncertainty_without_redispatch(revoked: bool) -> None:
    """期限は停止証明にならず、回収後も同じ呼出しを再実行できない。"""

    session = RequestSession()
    request = await accept(session)
    owner = await session.ledger().claim(request.request_id)
    assert owner is not None
    await session.ledger().start_call(owner, ordinal=0, feedback=None)
    session.requests[0].claimed_at = datetime.now(UTC) - timedelta(hours=1)
    if revoked:
        session.auth_session.revoked_at = datetime.now(UTC)
    cutoff = datetime.now(UTC) - timedelta(minutes=20)
    assert await session.ledger().recover_unknown(before=cutoff, limit=10) == 1
    assert session.requests[0].status == ("REVOKED" if revoked else "UNKNOWN")
    assert session.calls[0].returned_at is None
    assert len([row for row in session.rows if isinstance(row, OutboxMessage)]) == 1
    assert await session.ledger().claim(request.request_id) is None
    assert await session.ledger().recover_unknown(before=cutoff, limit=10) == 0


@pytest.mark.asyncio
async def test_recovery_rechecks_status_after_candidate_query() -> None:
    """候補列挙と lock の間で終わった要求を UNKNOWN に戻さない。"""

    session = RequestSession()
    request = await accept(session)
    owner = await session.ledger().claim(request.request_id)
    assert owner is not None
    session.requests[0].claimed_at = datetime.now(UTC) - timedelta(hours=1)

    def finished(point: str) -> None:
        """別 transaction の完了を候補検索直後に注入する。"""

        if point.startswith("recovery-candidates:"):
            session.requests[0].status = "FAILED"
            session.requests[0].error_code = "source_unavailable"
            session.requests[0].finished_at = datetime.now(UTC)

    session.on_step = finished
    assert (
        await session.ledger().recover_unknown(
            before=datetime.now(UTC) - timedelta(minutes=20), limit=10
        )
        == 0
    )
    assert session.requests[0].status == "FAILED"
