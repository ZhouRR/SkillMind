"""本番解釈器/SkillService/台帳を接続し、外部 model だけを合成 completion に置換する。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.db.models import OutboxMessage, SkillInterpretationCall, SkillInterpretationRequest
from skillmind.skills.domain import SkillInterpretationStatus, SkillSourceIntegrityError
from skillmind.skills.interpretation_requests import InterpretationRequestDeniedError
from skillmind.skills.interpreter import (
    load_capability_catalog,
    load_inline_text_files,
    load_interpreter_system_skill,
)
from skillmind.skills.model_interpreter import ModelCompletion
from skillmind.skills.request_execution import RequestCallControl
from skillmind.skills.request_service import InterpretationRequestService
from skillmind.skills.service import SkillService
from tests.skills.interpretation_request_harness import RequestSession
from tests.skills.test_model_interpreter import _interpreter, _response_schema
from tests.skills.test_skill_service import (
    CATALOG,
    CONTRACTS,
    GENERIC_SKILL,
    SYSTEM_SKILL,
    _example_response,
)


class CompletionClient:
    """DB lock の外でのみ合成候補を返し、call/return 間の撤権を注入する。"""

    def __init__(self, session: RequestSession, *, repair: bool = False) -> None:
        """候補本文を log に出さず、回数と注入点だけを保持する。"""

        self.session = session
        self.repair = repair
        self.calls: list[dict[str, Any]] = []
        self.on_call: Callable[[], None] | None = None
        self.error: BaseException | None = None

    async def complete(self, **kwargs: Any) -> ModelCompletion:
        """実 permission が保存済みであり、model I/O 中に transaction が無いことを確認する。"""

        assert not self.session.transaction_active
        assert len(self.session.calls) == len(self.calls) + 1
        assert self.session.calls[-1].returned_at is None
        self.calls.append(kwargs)
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        value = (
            {"response_version": "wrong"}
            if self.repair and len(self.calls) == 1
            else _example_response()
        )
        return ModelCompletion(structured_output=value, text=None, truncated=False)


def service(session: RequestSession, client: CompletionClient) -> SkillService:
    """実 parser、生成 Schema、ModelSkillInterpreter と原要求 transaction を使う。"""

    return SkillService(
        cast(async_sessionmaker[AsyncSession], lambda: session),
        CONTRACTS,
        interpreter=_interpreter(client),
        capability_catalog=load_capability_catalog(CATALOG),
        interpreter_identity=load_interpreter_system_skill(
            SYSTEM_SKILL, generation_schema=_response_schema()
        ),
        default_model="synthetic-model",
    )


async def accept(session: RequestSession, workflow: SkillService):
    """既存の合成 Skill を本番 import 経路で保存し、原请求を受理する。"""

    package = workflow._parser.parse_directory(GENERIC_SKILL)
    source = await workflow.save_inline(
        access=session.access, files=load_inline_text_files(GENERIC_SKILL, package)
    )
    return await workflow.accept_interpretation_request(
        access=session.access, request_id=uuid4(), skill_source_id=source.skill_source_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [False, True])
async def test_request_runs_through_model_validation_and_atomic_result_once(repair: bool) -> None:
    """受理は model を呼ばず、初回/修復の各 return を保存してから結果を採用する。"""

    session = RequestSession()
    client = CompletionClient(session, repair=repair)
    workflow = service(session, client)
    request = await accept(session, workflow)
    assert not client.calls and request.status == "QUEUED"
    frozen = deepcopy(session.requests[0].input_json)
    result = await workflow.execute_interpretation_request(request.request_id)
    assert result is not None and result.status is SkillInterpretationStatus.PREVIEW_READY
    assert session.requests[0].interpretation_id == result.interpretation_id
    assert session.requests[0].status == "SUCCEEDED"
    assert len(client.calls) == (2 if repair else 1)
    assert all(call.returned_at is not None for call in session.calls)
    assert session.requests[0].input_json == frozen
    assert await workflow.execute_interpretation_request(request.request_id) is None
    assert len(client.calls) == (2 if repair else 1)


@pytest.mark.asyncio
async def test_prompt_notification_revocation_prevents_the_actual_completion() -> None:
    """prompt 通知の非同期待機で撤権されたら、許可も model 呼出しも発生しない。"""

    session = RequestSession()
    client = CompletionClient(session)
    workflow = service(session, client)
    request = await accept(session, workflow)

    async def progress(event, _data):
        """表示専用 callback の遅延を、実資格の失効で表す。"""

        if event == "interpret.prompt":
            session.auth_session.revoked_at = datetime.now(UTC)

    assert (
        await workflow.execute_interpretation_request(request.request_id, on_event=progress) is None
    )
    assert not client.calls and not session.calls
    assert session.requests[0].status == "REVOKED"


@pytest.mark.asyncio
async def test_completion_revocation_keeps_return_and_rejects_candidate() -> None:
    """候補を受信しても、その間に撤権された要求へ成果を採用しない。"""

    session = RequestSession()
    client = CompletionClient(session)
    workflow = service(session, client)
    request = await accept(session, workflow)
    before = len(session.interpretations)
    client.on_call = lambda: setattr(session.auth_session, "revoked_at", datetime.now(UTC))
    assert await workflow.execute_interpretation_request(request.request_id) is None
    assert len(client.calls) == 1 and session.calls[0].returned_at is not None
    assert session.requests[0].status == "REVOKED"
    assert len(session.interpretations) == before


@pytest.mark.asyncio
async def test_cancelled_completion_stays_unknown_and_is_not_restarted() -> None:
    """取消は return や停止を補造せず、再配送による model 呼出しを止める。"""

    session = RequestSession()
    client = CompletionClient(session)
    workflow = service(session, client)
    request = await accept(session, workflow)
    client.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await workflow.execute_interpretation_request(request.request_id)
    assert session.requests[0].status == "UNKNOWN"
    assert session.calls[0].returned_at is None
    assert await workflow.execute_interpretation_request(request.request_id) is None
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_source_drift_is_rejected_before_model_and_original_input_is_preserved() -> None:
    """受理後の source 改変を再解釈で隠さず、元入力を残して開始を拒否する。"""

    session = RequestSession()
    client = CompletionClient(session)
    workflow = service(session, client)
    request = await accept(session, workflow)
    frozen = deepcopy(session.requests[0].input_json)
    session.sources[0].content_hash = "sha256:" + "f" * 64
    with pytest.raises(SkillSourceIntegrityError):
        await workflow.execute_interpretation_request(request.request_id)
    assert not client.calls and not session.calls
    assert session.requests[0].input_json == frozen
    assert session.requests[0].status == "FAILED"
    assert session.requests[0].error_code == "input_integrity"


@pytest.mark.asyncio
async def test_existing_result_is_adopted_without_a_new_completion() -> None:
    """旧確定結果があれば、呼出しを補造せず原要求へ採用して終態にする。"""

    session = RequestSession()
    client = CompletionClient(session)
    workflow = service(session, client)
    request = await accept(session, workflow)
    original = await workflow.execute_interpretation_request(request.request_id)
    assert original is not None
    # 旧 DB を表す合成 fixture。成果は残し、新 migration の空台帳から受理する。
    session.rows = tuple(
        row
        for row in session.rows
        if not isinstance(row, (SkillInterpretationRequest, SkillInterpretationCall, OutboxMessage))
    )
    reused_request = await workflow.accept_interpretation_request(
        access=session.access, request_id=uuid4(), skill_source_id=request.skill_source_id
    )
    reused = await workflow.execute_interpretation_request(reused_request.request_id)
    assert reused is not None and reused.interpretation_id == original.interpretation_id
    assert reused.reused and session.requests[0].status == "SUCCEEDED"
    assert not session.calls and len(client.calls) == 1


@pytest.mark.asyncio
async def test_adjustment_freezes_original_actor_and_parent_without_mutating_parent() -> None:
    """調整も同じ台帳に入り、親成果と source は不変のまま新しい子成果を保存する。"""

    session = RequestSession()
    client = CompletionClient(session)
    workflow = service(session, client)
    request = await accept(session, workflow)
    parent = await workflow.execute_interpretation_request(request.request_id)
    assert parent is not None
    original_manifest = deepcopy(parent.preview.runtime_manifest_draft)
    adjusted = await workflow.accept_interpretation_request(
        access=session.access,
        request_id=uuid4(),
        parent_interpretation_id=parent.interpretation_id,
        instruction="Keep the original evidence trace.",
    )
    assert adjusted.input["adjustment"]["actor_id"] == str(session.user.id)
    child = await workflow.execute_interpretation_request(adjusted.request_id)
    assert child is not None and child.parent_interpretation_id == parent.interpretation_id
    assert child.interpretation_id != parent.interpretation_id
    assert parent.preview.runtime_manifest_draft == original_manifest
    assert len(client.calls) == 2 and len(session.calls) == 2


@pytest.mark.asyncio
async def test_controller_never_retries_a_call_grant_after_unknown_commit() -> None:
    """commit が rollback だった場合でも同じ controller は許可の再試行を行わない。"""

    session = RequestSession()
    client = CompletionClient(session)
    workflow = service(session, client)
    request = await accept(session, workflow)
    ledger = InterpretationRequestService(workflow._session_factory)
    owner = await ledger.claim(request.request_id)
    assert owner is not None
    control = RequestCallControl(ledger, owner)
    session.commit_outcome = "not-committed"
    with pytest.raises(ConnectionError):
        await control.before_call(feedback=None)
    session.commit_outcome = None
    with pytest.raises(InterpretationRequestDeniedError):
        await control.before_call(feedback=None)
    assert not client.calls and not session.calls


@pytest.mark.asyncio
async def test_preparation_error_after_a_call_is_not_misclassified_as_never_started() -> None:
    """同じ例外型でも許可が既にあれば、モデル未開始の確定失敗にはしない。"""

    session = RequestSession()
    client = CompletionClient(session)
    workflow = service(session, client)
    request = await accept(session, workflow)
    client.error = SkillSourceIntegrityError("Synthetic failure after the call grant")
    with pytest.raises(SkillSourceIntegrityError):
        await workflow.execute_interpretation_request(request.request_id)
    assert session.requests[0].status == "UNKNOWN"
    assert len(client.calls) == 1 and session.calls[0].returned_at is None


@pytest.mark.asyncio
async def test_expired_preparation_failure_keeps_revocation_instead_of_candidate_failure() -> None:
    """準備失敗の最終 flush 中に失効した場合も、元要求の撤権を優先して残す。"""

    session = RequestSession()
    client = CompletionClient(session)
    workflow = service(session, client)
    request = await accept(session, workflow)
    ledger = InterpretationRequestService(workflow._session_factory)
    owner = await ledger.claim(request.request_id)
    assert owner is not None
    session.on_step = lambda point: (
        setattr(session.auth_session, "idle_expires_at", datetime.now(UTC))
        if point.startswith("flush:")
        else None
    )
    assert not await ledger.fail_before_call(owner, error_code="input_integrity")
    assert session.requests[0].status == "REVOKED"
    assert session.requests[0].error_code == "authorization_revoked"
