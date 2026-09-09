"""普通回答の版、原 intent、HTTP 結果と旧承認 detail の互換性を fake service で検証する。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fakes import DeniedProjectAuthorizationService, FakeAuthService
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from projectmind.api.routes.runs import RespondInteractionResponse
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.projects import ProjectStatus
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from projectmind.runs.domain import (
    CreatedRun,
    InteractionConflictError,
    InteractionExpiredError,
    InteractionNotFoundError,
    InteractionResponseInvalidError,
    RespondedInteraction,
    RunDetail,
    RunNotFoundError,
    RunStatus,
    SessionContinuationMode,
    StoredInteractionResponse,
    StoredUserInteraction,
    UserInteractionStatus,
    UserInteractionType,
)
from projectmind.runs.service import RunService
from projectmind.users.domain import UserAccess

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _result(*, replay: bool = False, status: RunStatus = RunStatus.QUEUED) -> RespondedInteraction:
    """副作用の無い固定 response projection を作る。"""

    return RespondedInteraction(
        run=CreatedRun(
            run_id=uuid4(),
            project_id=uuid4(),
            task_id=uuid4(),
            status=status,
            row_version=7,
            created_at=datetime.now(UTC),
            idempotent_replay=False,
        ),
        interaction_id=uuid4(),
        response_id=uuid4(),
        run_segment_id=uuid4(),
        segment_no=2,
        continuation_mode=SessionContinuationMode.FORK,
        idempotent_replay=replay,
    )


def _url(result: RespondedInteraction) -> str:
    """原 Project/Run/Interaction identity を URL に固定する。"""

    return (
        f"/api/v1/projects/{result.run.project_id}/runs/{result.run.run_id}"
        f"/interactions/{result.interaction_id}/responses"
    )


def _service(client: TestClient, result: RespondedInteraction) -> MagicMock:
    """Business transaction を実行しない service mock を配線する。"""

    service = MagicMock(spec=RunService)
    service.respond_to_interaction = AsyncMock(return_value=result)
    client.app.state.run_service = service
    return service


def _assert_private_response(response: Any) -> None:
    """成功と Problem の双方で cache 禁止と追跡 ID を確認する。"""

    assert response.headers["Cache-Control"] == "no-store"
    UUID(response.headers["X-Request-ID"])


@pytest.mark.parametrize("status", list(RunStatus))
def test_original_replay_returns_current_run_and_exact_replay_header(
    client: TestClient,
    status: RunStatus,
) -> None:
    """200 は現在 Run 状態と原回答/Segment ID を返し、原本文の空白と選択順を保つ。"""

    result = _result(replay=True, status=status)
    service = _service(client, result)
    answer = {"text": "  Keep this.  ", "selected_option_keys": ["second", "first"]}
    response = client.post(
        _url(result),
        headers={"Idempotency-Key": "original-key"},
        json={"interaction_version": 1, "response": answer},
    )
    assert response.status_code == 200
    assert response.headers["Idempotent-Replay"] == "true"
    _assert_private_response(response)
    body = response.json()
    assert body == {
        "run_id": str(result.run.run_id),
        "project_id": str(result.run.project_id),
        "status": status.value,
        "row_version": 7,
        "interaction_id": str(result.interaction_id),
        "response_id": str(result.response_id),
        "run_segment_id": str(result.run_segment_id),
        "segment_no": 2,
        "continuation_mode": "FORK",
        "idempotent_replay": True,
    }
    service.respond_to_interaction.assert_awaited_once_with(
        project_id=result.run.project_id,
        run_id=result.run.run_id,
        interaction_id=result.interaction_id,
        access=UserAccess(
            actor=client.app.state.auth_service.actor,
            request_id=UUID(response.headers["X-Request-ID"]),
            session_token="",
            csrf_token=client.headers["X-CSRF-Token"],
        ),
        interaction_version=1,
        response_json=answer,
        idempotency_key="original-key",
        trace_id=response.headers["X-Request-ID"],
    )
    schema = json.loads(
        (CONTRACTS / "runs/interaction-response/v1/response.schema.json").read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(body)


def test_first_response_returns_201_and_does_not_trim_legacy_text(client: TestClient) -> None:
    """minLength 1 の旧契約を維持し、新規 UI の非空白規則を API の hash へ混入しない。"""

    result = _result()
    service = _service(client, result)
    response = client.post(
        _url(result),
        headers={"Idempotency-Key": "first-key"},
        json={"interaction_version": 1, "response": {"text": " "}},
    )
    assert response.status_code == 201 and response.json()["status"] == "QUEUED"
    assert response.headers["Idempotent-Replay"] == "false"
    assert response.json()["idempotent_replay"] is False
    _assert_private_response(response)
    assert service.respond_to_interaction.await_args.kwargs["response_json"] == {"text": " "}


def test_response_passes_original_cookie_and_csrf_only_in_internal_access(
    client: TestClient,
) -> None:
    """入口 actor の再利用だけでなく、原 credential と server request UUID を業務へ渡す。"""

    result = _result()
    service = _service(client, result)
    synthetic_cookie = "synthetic-original-cookie-for-test"
    client.cookies.set(client.app.state.settings.auth_session_cookie_name, synthetic_cookie)
    response = client.post(
        _url(result), headers={"Idempotency-Key": "original-proof"},
        json={"interaction_version": 1, "response": {"text": "Continue"}},
    )
    assert response.status_code == 201
    arguments = service.respond_to_interaction.await_args.kwargs
    assert "actor_id" not in arguments
    assert arguments["access"] == UserAccess(
        actor=client.app.state.auth_service.actor,
        request_id=UUID(response.headers["X-Request-ID"]),
        session_token=synthetic_cookie,
        csrf_token=client.headers["X-CSRF-Token"],
    )
    assert synthetic_cookie not in response.text
    assert client.headers["X-CSRF-Token"] not in response.text
    _assert_private_response(response)


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 0, -1, None])
def test_answer_version_is_strict_before_service(client: TestClient, version: object) -> None:
    """bool、文字、浮動小数と非正数を version 1 と同一視しない。"""

    result = _result()
    service = _service(client, result)
    response = client.post(
        _url(result),
        headers={"Idempotency-Key": "first-key"},
        json={"interaction_version": version, "response": {"text": "Continue"}},
    )
    assert response.status_code == 422 and response.json()["code"] == "validation_error"
    _assert_private_response(response)
    service.respond_to_interaction.assert_not_awaited()


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"text": None},
        {"text": ""},
        {"text": 1},
        {"text": "x" * 10001},
        {"selected_option_keys": None},
        {"selected_option_keys": []},
        {"selected_option_keys": ["same", "same"]},
        {"selected_option_keys": ["Bad"]},
        {"selected_option_keys": [1]},
        {"selected_option_keys": ["a" * 129]},
        {"selected_option_keys": [f"key-{index}" for index in range(21)]},
        {"text": "Continue", "unexpected": True},
    ],
)
def test_invalid_answer_shape_never_reaches_service(client: TestClient, answer: object) -> None:
    """公開 Schema の空回答、明示 null、未知欄、選択規則を入口で強制する。"""

    result = _result()
    service = _service(client, result)
    response = client.post(
        _url(result),
        headers={"Idempotency-Key": "first-key"},
        json={"interaction_version": 1, "response": answer},
    )
    assert response.status_code == 422
    assert response.headers["Content-Type"].startswith("application/problem+json")
    _assert_private_response(response)
    service.respond_to_interaction.assert_not_awaited()


@pytest.mark.parametrize("missing", ["interaction_version", "response", "Idempotency-Key"])
def test_required_request_parts_are_not_invented(client: TestClient, missing: str) -> None:
    """原版、回答、原 key の欠落を default 値で補わない。"""

    result = _result()
    service = _service(client, result)
    body: dict[str, Any] = {"interaction_version": 1, "response": {"text": "Continue"}}
    body.pop(missing, None)
    response = client.post(
        _url(result),
        json=body,
        headers={} if missing == "Idempotency-Key" else {"Idempotency-Key": "first-key"},
    )
    assert response.status_code == 422
    _assert_private_response(response)
    service.respond_to_interaction.assert_not_awaited()


def test_duplicate_original_keys_are_not_silently_selected(client: TestClient) -> None:
    """同じ HTTP request の原 key が曖昧な場合、先頭/末尾を勝手に選ばない。"""

    result = _result()
    service = _service(client, result)
    response = client.post(
        _url(result),
        headers=[("Idempotency-Key", "first"), ("Idempotency-Key", "second")],
        json={"interaction_version": 1, "response": {"text": "Continue"}},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "interaction_response_invalid"
    _assert_private_response(response)
    service.respond_to_interaction.assert_not_awaited()


@pytest.mark.parametrize(
    "error,status,code",
    [
        (InteractionNotFoundError, 404, "run_not_found"),
        (InteractionConflictError, 409, "interaction_conflict"),
        (InteractionExpiredError, 410, "interaction_expired"),
        (InteractionResponseInvalidError, 422, "interaction_response_invalid"),
        (UnauthorizedSessionError, 401, "authentication_required"),
        (CsrfRejectedError, 403, "csrf_rejected"),
        (ProjectNotFoundError, 404, "project_not_found"),
        (ProjectArchivedError, 409, "project_archived"),
    ],
)
def test_domain_rejections_keep_problem_and_no_success_replay_header(
    client: TestClient,
    error: type[Exception],
    status: int,
    code: str,
) -> None:
    """期限切れを再試行可能な成功扱いせず、既知拒否の Problem code を固定する。"""

    result = _result()
    service = _service(client, result)
    service.respond_to_interaction.side_effect = error("Rejected")
    response = client.post(
        _url(result),
        headers={"Idempotency-Key": "first-key"},
        json={"interaction_version": 1, "response": {"text": "Continue"}},
    )
    assert response.status_code == status and response.json()["code"] == code
    assert response.headers["Content-Type"].startswith("application/problem+json")
    assert "Idempotent-Replay" not in response.headers
    _assert_private_response(response)


@pytest.mark.parametrize(
    "boundary,status,code",
    [
        ("session", 401, "authentication_required"),
        ("csrf", 403, "csrf_rejected"),
        ("origin", 403, "csrf_rejected"),
        ("project", 404, "project_not_found"),
        ("archived", 409, "project_archived"),
    ],
)
def test_write_authentication_and_project_boundary_precedes_response_service(
    client: TestClient,
    boundary: str,
    status: int,
    code: str,
) -> None:
    """現在入口の session/CSRF/membership/archive 拒否を確認し、後続 transaction と区別する。"""

    result = _result()
    service = _service(client, result)
    headers = {"Idempotency-Key": "first-key"}
    if boundary == "session":
        client.app.state.auth_service = FakeAuthService(unauthorized=True)
    elif boundary == "csrf":
        headers["X-CSRF-Token"] = "invalid"
    elif boundary == "origin":
        headers["Origin"] = "https://foreign.invalid"
    elif boundary == "project":
        client.app.state.project_service = DeniedProjectAuthorizationService()
    else:
        getter = client.app.state.project_service.get_project

        async def archived(**kwargs: Any) -> Any:
            """同じ authorized Project が archive 済みの入口を模倣する。"""

            return replace(await getter(**kwargs), status=ProjectStatus.ARCHIVED)

        client.app.state.project_service.get_project = archived
    response = client.post(
        _url(result),
        headers=headers,
        json={"interaction_version": 1, "response": {"text": "Continue"}},
    )
    assert response.status_code == status and response.json()["code"] == code
    _assert_private_response(response)
    service.respond_to_interaction.assert_not_awaited()


@pytest.mark.parametrize("kind", list(UserInteractionType))
@pytest.mark.parametrize("has_response", [False, True])
def test_detail_keeps_shared_interaction_types_and_original_response_audit(
    client: TestClient,
    kind: UserInteractionType,
    has_response: bool,
) -> None:
    """旧 orphan 批准を含む詳細は読取可能に保ち、原作者と原版を改造しない。"""

    result = _result()
    service = _service(client, result)
    now = datetime.now(UTC)
    original = StoredInteractionResponse(uuid4(), uuid4(), 1, {"text": "  Original  "}, now)
    interaction = StoredUserInteraction(
        interaction_id=result.interaction_id,
        run_segment_id=uuid4(),
        agent_session_id=uuid4(),
        interaction_type=kind,
        prompt={"prompt": "Review"},
        options=(),
        required=False,
        expires_at=now,
        status=UserInteractionStatus.RESPONDED if has_response else UserInteractionStatus.OPEN,
        version=2 if has_response else 1,
        continuation_mode=SessionContinuationMode.RESUME,
        checkpoint_checksum="sha256:" + "a" * 64,
        change_proposal_id=None,
        response=original if has_response else None,
        created_at=now,
    )
    detail = RunDetail(
        run=result.run,
        input={},
        selected_sources={},
        result=None,
        tool_calls=(),
        evidence=(),
        interactions=(interaction,),
    )
    service.get_run_detail = AsyncMock(return_value=detail)
    response = client.get(
        f"/api/v1/projects/{result.run.project_id}/runs/{result.run.run_id}/detail"
    )
    assert response.status_code == 200
    _assert_private_response(response)
    item = response.json()["interactions"][0]
    assert item["interaction_type"] == kind.value and item["change_proposal_id"] is None
    if has_response:
        assert item["response"]["actor_id"] == str(original.actor_id)
        assert item["response"]["interaction_version"] == 1
        assert item["response"]["response"] == original.response
    else:
        assert item["response"] is None
    service.respond_to_interaction.assert_not_awaited()


def test_detail_missing_scoped_run_is_private_404(client: TestClient) -> None:
    """別 Project/不存在 Run の詳細を service ownership 拒否から共通 404 に畳む。"""

    result = _result()
    service = _service(client, result)
    service.get_run_detail = AsyncMock(side_effect=RunNotFoundError("Not found"))
    response = client.get(
        f"/api/v1/projects/{result.run.project_id}/runs/{result.run.run_id}/detail"
    )
    assert response.status_code == 404 and response.json()["code"] == "run_not_found"
    _assert_private_response(response)


@pytest.mark.parametrize(
    "field,value",
    [
        ("row_version", True),
        ("row_version", 0),
        ("segment_no", 1),
        ("segment_no", 2.0),
        ("idempotent_replay", "false"),
        ("continuation_mode", "INITIAL"),
        ("status", "RUNNING"),
        ("unexpected", "field"),
    ],
)
def test_success_projection_rejects_invalid_shape(field: str, value: object) -> None:
    """Backend 自身の誤った first response を公開 success として整形しない。"""

    body = json.loads((CONTRACTS / "examples/interaction-response.v1.json").read_text())
    body[field] = value
    with pytest.raises(ValidationError):
        RespondInteractionResponse.model_validate(body)


def test_interaction_openapi_declares_exact_success_headers_and_problem_models(
    client: TestClient,
) -> None:
    """生成仕様に 201/200、cache 禁止、全文 Problem と ordinary/shared 型の差を固定する。"""

    document = client.get("/api/openapi.json").json()
    operation = document["paths"][
        "/api/v1/projects/{project_id}/runs/{run_id}/interactions/{interaction_id}/responses"
    ]["post"]
    for status, replay in (("201", "false"), ("200", "true")):
        response = operation["responses"][status]
        assert response["headers"]["Idempotent-Replay"]["schema"]["const"] == replay
        assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
        assert "application/json" in response["content"]
    for status in ("401", "403", "404", "409", "410", "422"):
        response = operation["responses"][status]
        assert "application/problem+json" in response["content"]
        assert "application/json" not in response["content"]
        assert "X-Request-ID" in response["headers"]
    answer = document["components"]["schemas"]["InteractionAnswer"]
    assert answer["additionalProperties"] is False and len(answer["anyOf"]) == 2
    assert answer["properties"]["text"]["type"] == "string"
    assert "default" not in answer["properties"]["text"]
    assert answer["properties"]["selected_option_keys"]["uniqueItems"] is True
    assert "EFFECT_APPROVAL" in document["components"]["schemas"]["UserInteractionType"]["enum"]
