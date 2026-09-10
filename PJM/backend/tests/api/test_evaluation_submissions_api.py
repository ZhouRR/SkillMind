"""原 Evaluation 回执・確認・ページの HTTP/認可/静的失敗契約を検証する。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from typing import Any, Protocol, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fakes import DeniedProjectAuthorizationService, FakeAuthService, FakeEvaluationService
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request

from projectmind.api.routes import evaluations as routes
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.evaluations.domain import (
    CreateEvaluationCommand,
    EvaluationIntegrityError,
    EvaluationResultMismatchError,
    EvaluationResultNotFoundError,
    EvaluationSubmissionConflictError,
    EvaluationSubmissionNotFoundError,
    EvaluationVerdict,
    InvalidEvaluationCommandError,
    InvalidEvaluationCursorError,
    InvalidEvaluationRevisionError,
    StoredEvaluationPage,
    StoredEvaluationSubmission,
)
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError, ProjectStatus
from projectmind.runs.domain import RunNotFoundError
from projectmind.users.domain import UserAccess

OPERATIONS = ("legacy_post", "legacy_get", "submit", "confirm", "page")
PRIVATE = "synthetic-private-must-not-be-published"


class EvaluationHttpResponse(Protocol):
    """TestClient の実装名ではなく、この回帰が読む HTTP response 面だけに依存する。"""

    @property
    def status_code(self) -> int:
        """実際の HTTP status を読む。"""

    @property
    def headers(self) -> Mapping[str, str]:
        """実際の cache/Problem header を読む。"""

    @property
    def text(self) -> str:
        """公開本文に private な例外が含まれないことを検証する。"""

    def json(self) -> Any:
        """JSON の field は各 assertion で契約に沿って検証する。"""


def _install(client: TestClient) -> tuple[str, dict[str, Any], FakeEvaluationService]:
    """既存の認証 client を使い、Project/Run/Result を一つの合成 scope に固定する。"""

    project_id, run_id = uuid4(), uuid4()
    service = FakeEvaluationService()
    service.scope = (project_id, run_id)
    app = cast(FastAPI, client.app)
    app.state.evaluation_service = service
    client.cookies.set(app.state.settings.auth_session_cookie_name, "synthetic-session")
    body = {
        "submission_key": str(uuid4()), "result_id": str(service.result_id),
        "rating": 4, "verdict": "partially_accurate",
    }
    return f"/api/v1/projects/{project_id}/runs/{run_id}", body, service


def _call(
    client: TestClient, base: str, body: dict[str, Any], operation: str,
) -> EvaluationHttpResponse:
    """新旧入口へ同じ原 scope を渡し、fixture 側で POST を自動再送しない。"""

    if operation == "legacy_post":
        return client.post(f"{base}/evaluations", json={
            key: value for key, value in body.items() if key not in {"submission_key", "result_id"}
        })
    if operation == "legacy_get":
        return client.get(f"{base}/evaluations")
    if operation == "submit":
        return client.post(f"{base}/evaluation-submissions", json=body)
    if operation == "confirm":
        return client.get(f"{base}/evaluation-submissions/{body['submission_key']}", params={
            "result_id": body["result_id"],
        })
    assert operation == "page"
    return client.get(f"{base}/evaluations/page")


def test_first_submission_replay_and_confirmation_return_one_original_receipt(
    client: TestClient,
) -> None:
    """省略 default と明示 default は同じ原要求であり、確認も追加評価を作らない。"""

    base, body, service = _install(client)
    first = _call(client, base, body, "submit")
    repeated = _call(client, base, {**body, "comment": "", "revisions": []}, "submit")
    confirmed = _call(client, base, body, "confirm")

    assert (first.status_code, repeated.status_code, confirmed.status_code) == (201, 200, 200)
    assert first.json() == repeated.json() == confirmed.json()
    assert set(first.json()) == {"project_id", "run_id", "submission_key", "evaluation"}
    assert len(first.json()["evaluation"]) == 9
    assert len(service.items) == 1
    assert first.json()["evaluation"]["result_id"] == body["result_id"]
    assert first.json()["evaluation"]["user_id"] == str(service.accesses[0].actor.user_id)
    for response in (first, repeated, confirmed):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-request-id"]
        assert "idempotent_replay" not in response.text and "request_hash" not in response.text
    assert all(access.session_token == "synthetic-session" for access in service.accesses)
    assert len({access.request_id for access in service.accesses}) == 3


def test_old_post_remains_additive_and_unrelated_history_never_confirms_new_key(
    client: TestClient,
) -> None:
    """既存 request を幂等版と誤認せず、類似履歴があっても未登録 key は 404 にする。"""

    base, body, service = _install(client)
    first = _call(client, base, body, "legacy_post")
    second = _call(client, base, body, "legacy_post")
    confirmed = _call(client, base, body, "confirm")

    assert first.status_code == second.status_code == 201
    assert first.json()["evaluation_id"] != second.json()["evaluation_id"]
    assert confirmed.status_code == 404
    assert confirmed.json()["code"] == "evaluation_submission_not_found"
    assert len(service.items) == 2
    assert set(_call(client, base, body, "legacy_get").json()) == {"items"}


@pytest.mark.parametrize("change", [
    {"rating": 2}, {"verdict": "uncertain"}, {"comment": " "},
    {"revisions": [{"pointer": "/summary", "suggested_value": None, "reason": "Review"}]},
])
def test_same_key_different_content_conflicts_without_replacing_original(
    client: TestClient, change: dict[str, Any],
) -> None:
    """原値と異なる request は 409 で止め、元の回执を保持する。"""

    base, body, service = _install(client)
    original = _call(client, base, body, "submit").json()
    response = _call(client, base, {**body, **change}, "submit")
    assert response.status_code == 409
    assert response.json()["code"] == "evaluation_submission_conflict"
    assert _call(client, base, body, "confirm").json() == original
    assert len(service.items) == 1


def test_revision_order_and_original_strings_are_not_normalized_away(client: TestClient) -> None:
    """文字列の空白と修訂配列の順序を保ち、JSON object の key 順序だけは同一視する。"""

    base, body, service = _install(client)
    body.update(comment="  原コメント  ", revisions=[
        {"pointer": "/summary", "suggested_value": {"a": None, "b": True}, "reason": " 理由 "},
        {"pointer": "/structured_data", "suggested_value": None, "reason": "Second"},
    ])
    original = _call(client, base, body, "submit")
    assert original.status_code == 201
    assert original.json()["evaluation"]["comment"] == "  原コメント  "
    assert original.json()["evaluation"]["revisions"][0]["reason"] == " 理由 "
    copy = deepcopy(body)
    copy["revisions"][0]["suggested_value"] = {"b": True, "a": None}
    assert _call(client, base, copy, "submit").status_code == 200
    copy["revisions"].reverse()
    assert _call(client, base, copy, "submit").status_code == 409
    assert len(service.items) == 1


def test_confirmation_requires_original_actor_but_allows_new_session_without_csrf(
    client: TestClient,
) -> None:
    """原 key は他 actor の内容発見に使えず、同 actor は新会話で安全に確認できる。"""

    base, body, service = _install(client)
    original = _call(client, base, body, "submit").json()
    auth = cast(FastAPI, client.app).state.auth_service
    original_actor = auth.actor
    auth.actor = replace(auth.actor, user_id=uuid4())
    assert _call(client, base, body, "confirm").status_code == 404
    auth.actor = original_actor
    client.cookies.set(cast(FastAPI, client.app).state.settings.auth_session_cookie_name,
                       "synthetic-new-session")
    client.headers.pop("X-CSRF-Token", None)
    confirmed = _call(client, base, body, "confirm")
    assert confirmed.status_code == 200 and confirmed.json() == original
    assert service.accesses[-1].session_token == "synthetic-new-session"
    assert service.accesses[-1].csrf_token == ""
    assert len(service.items) == 1


@pytest.mark.asyncio
async def test_archived_project_allows_confirmation_and_page_but_refuses_post(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """帰档は新 mutation を拒否するが、現在の資格を持つ原要求の読取は保つ。"""

    base, body, service = _install(client)
    original = _call(client, base, body, "submit").json()
    app = cast(FastAPI, client.app)
    assert service.scope is not None
    project = await app.state.project_service.get_project(
        actor=app.state.auth_service.actor, project_id=service.scope[0],
    )
    monkeypatch.setattr(app.state.project_service, "get_project", AsyncMock(
        return_value=replace(project, status=ProjectStatus.ARCHIVED),
    ))
    assert _call(client, base, body, "confirm").json() == original
    assert _call(client, base, body, "page").status_code == 200
    assert _call(client, base, body, "submit").json()["code"] == "project_archived"
    assert len(service.items) == 1


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure,code,status", [
    (UnauthorizedSessionError(PRIVATE), "authentication_required", 401),
    (CsrfRejectedError(PRIVATE), "csrf_rejected", 403),
    (ProjectNotFoundError(PRIVATE), "project_not_found", 404),
    (RunNotFoundError(PRIVATE), "run_not_found", 404),
    (EvaluationResultNotFoundError(PRIVATE), "result_not_available", 409),
    (EvaluationIntegrityError(PRIVATE), "evaluation_record_invalid", 503),
    (SQLAlchemyError(PRIVATE), "evaluation_storage_unavailable", 503),
    (TimeoutError(PRIVATE), "evaluation_storage_unavailable", 503),
    (ConnectionError(PRIVATE), "evaluation_storage_unavailable", 503),
])
def test_transaction_failures_preserve_status_and_never_publish_exception_content(
    client: TestClient, operation: str, failure: Exception, code: str, status: int,
) -> None:
    """入口認証後の失効・損傷・DB 故障も全経路で静的 Problem に揃える。"""

    base, body, service = _install(client)
    service.failure = failure
    response = _call(client, base, body, operation)
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert PRIVATE not in response.text
    assert service.items == [] and len(service.accesses) == 1


@pytest.mark.parametrize("failure,code,status", [
    (ProjectArchivedError(PRIVATE), "project_archived", 409),
    (InvalidEvaluationRevisionError(PRIVATE), "invalid_evaluation_revision", 400),
    (InvalidEvaluationCommandError(PRIVATE), "invalid_evaluation_request", 422),
    (EvaluationResultMismatchError(PRIVATE), "evaluation_result_mismatch", 409),
    (EvaluationSubmissionConflictError(PRIVATE), "evaluation_submission_conflict", 409),
    (EvaluationSubmissionNotFoundError(PRIVATE), "evaluation_submission_not_found", 404),
    (InvalidEvaluationCursorError(PRIVATE), "invalid_evaluation_cursor", 400),
])
def test_domain_rejections_use_frozen_static_codes(
    client: TestClient, failure: Exception, code: str, status: int,
) -> None:
    """詳細な pointer や実 Result identity は domain 例外から公開しない。"""

    base, body, service = _install(client)
    service.failure = failure
    response = _call(client, base, body, "submit")
    assert response.status_code == status and response.json()["code"] == code
    assert PRIVATE not in response.text


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("denied", ["session", "project"])
def test_dependencies_reject_before_service_call(
    client: TestClient, operation: str, denied: str,
) -> None:
    """共通 actor dependency が拒否した request を business use case へ渡さない。"""

    base, body, service = _install(client)
    app = cast(FastAPI, client.app)
    if denied == "session":
        app.state.auth_service = FakeAuthService(unauthorized=True)
    else:
        app.state.project_service = DeniedProjectAuthorizationService()
    response = _call(client, base, body, operation)
    assert response.status_code == (401 if denied == "session" else 404)
    assert response.headers["cache-control"] == "no-store"
    assert service.accesses == []


@pytest.mark.parametrize("field,value", [
    ("submission_key", str(UUID(int=0))), ("result_id", str(UUID(int=0))),
    ("submission_key", "invalid"), ("result_id", "invalid"),
    ("rating", True), ("rating", "4"), ("rating", 4.0),
    ("user_id", str(uuid4())), ("comment", None), ("revisions", None),
])
def test_new_submission_rejects_invalid_shape_before_business_call(
    client: TestClient, field: str, value: object,
) -> None:
    """新入口は UUID・厳密な rating・白名单を守り、代理 user_id を受け付けない。"""

    base, body, service = _install(client)
    response = _call(client, base, {**body, field: value}, "submit")
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert service.accesses == []


@pytest.mark.parametrize("cursor", ["", "not-a-uuid", str(UUID(int=0)), str(uuid4())])
def test_bad_cursor_has_one_400_contract(client: TestClient, cursor: str) -> None:
    """不正テキスト/nil/未保存 cursor を、framework の 422 と分裂させない。"""

    base, _body, service = _install(client)
    response = client.get(f"{base}/evaluations/page", params={"after": cursor})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_evaluation_cursor"
    assert response.headers["cache-control"] == "no-store"
    assert service.items == []


@pytest.mark.parametrize("limit", ["0", "101", "1.5", "invalid"])
def test_invalid_page_limit_is_422_before_business_call(client: TestClient, limit: str) -> None:
    """ページ上限の契約違反で全量 fallback や暗黙の切り詰めをしない。"""

    base, _body, service = _install(client)
    response = client.get(f"{base}/evaluations/page", params={"limit": limit})
    assert response.status_code == 422 and service.accesses == []


def test_page_returns_stable_cursor_and_legacy_shape_is_unchanged(client: TestClient) -> None:
    """同時刻の UUID 順序を通じて全履歴を続読し、最後の cursor だけ null にする。"""

    base, body, service = _install(client)
    for _ in range(3):
        assert _call(client, base, body, "legacy_post").status_code == 201
    first = client.get(f"{base}/evaluations/page", params={"limit": 2})
    assert first.status_code == 200
    payload = first.json()
    assert set(payload) == {"project_id", "run_id", "result_id", "items", "next_cursor"}
    assert len(payload["items"]) == 2
    assert payload["result_id"] == str(service.result_id)
    assert payload["next_cursor"] == payload["items"][-1]["evaluation_id"]
    second = client.get(f"{base}/evaluations/page", params={
        "limit": 2, "after": payload["next_cursor"],
    }).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    expected = sorted(str(item.evaluation_id) for item in service.items)
    assert [item["evaluation_id"] for item in payload["items"] + second["items"]] == expected
    assert set(_call(client, base, body, "legacy_get").json()) == {"items"}


@pytest.mark.parametrize("operation", ["submit", "confirm"])
@pytest.mark.parametrize("field", [
    "project_id", "run_id", "submission_key", "evaluation.run_id", "evaluation.result_id",
    "evaluation.user_id", "evaluation.rating", "idempotent_replay",
])
def test_wrong_receipt_scope_or_saved_type_never_reaches_the_caller(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operation: str, field: str,
) -> None:
    """SQL の選択に依存するだけでなく、公開回执でも URL/actor/Result を照合する。"""

    base, body, service = _install(client)
    assert _call(client, base, body, "submit").status_code == 201
    assert service.scope is not None
    submitted = StoredEvaluationSubmission(
        project_id=service.scope[0], run_id=service.scope[1],
        submission_key=UUID(body["submission_key"]), evaluation=service.items[0],
        idempotent_replay=True,
    )
    if field.startswith("evaluation."):
        target = field.removeprefix("evaluation.")
        value: Any = True if target == "rating" else uuid4()
        submitted = replace(submitted, evaluation=replace(submitted.evaluation, **{target: value}))
    else:
        value = 1 if field == "idempotent_replay" else uuid4()
        submitted = replace(submitted, **{field: value})
    monkeypatch.setattr(service, "submit" if operation == "submit" else "get_submission",
                        AsyncMock(return_value=submitted))
    response = _call(client, base, body, operation)
    assert response.status_code == 503
    assert response.json()["code"] == "evaluation_record_invalid"
    assert "evaluation" not in response.json()


def test_post_rejects_changed_receipt_content_and_preserves_independent_original_command(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """service が待機中に原 command を変更しても、同じ変更を回执照合の基準にしない。"""

    base, body, service = _install(client)
    body["revisions"] = [{"pointer": "/summary", "suggested_value": {"value": "original"},
                          "reason": "Review"}]
    assert _call(client, base, body, "submit").status_code == 201
    evaluation = service.items[0]
    assert service.scope is not None

    async def altered(
        command: CreateEvaluationCommand, *, submission_key: UUID, result_id: UUID,
        access: UserAccess,
    ) -> StoredEvaluationSubmission:
        """合成した境界違反で nested dict を書換え、公開側の独立した原内容を検証する。"""

        assert result_id == service.result_id and command.user_id == access.actor.user_id
        command.revisions[0].suggested_value["value"] = PRIVATE
        return StoredEvaluationSubmission(
            project_id=command.project_id, run_id=command.run_id,
            submission_key=submission_key,
            evaluation=replace(evaluation, revisions=(replace(
                evaluation.revisions[0], suggested_value={"value": PRIVATE},
            ),)), idempotent_replay=True,
        )

    monkeypatch.setattr(service, "submit", altered)
    response = _call(client, base, body, "submit")
    assert response.status_code == 503 and response.json()["code"] == "evaluation_record_invalid"
    assert PRIVATE not in response.text


@pytest.mark.parametrize("corruption", [
    "project", "run", "result", "item_run", "item_result", "duplicate", "reverse",
    "cursor", "oversize", "short_cursor_page", "after_in_page",
])
def test_mixed_or_unbounded_page_is_rejected_before_publication(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, corruption: str,
) -> None:
    """混在・逆順・過大・偽 cursor の保存 DTO を成功一覧に投影しない。"""

    base, body, service = _install(client)
    for _ in range(2):
        assert _call(client, base, body, "legacy_post").status_code == 201
    assert service.scope is not None
    items = tuple(sorted(service.items, key=lambda item: (item.created_at, item.evaluation_id)))
    page = StoredEvaluationPage(project_id=service.scope[0], run_id=service.scope[1],
                                result_id=service.result_id, items=items, next_cursor=None)
    params: dict[str, str | int] = {"limit": 2}
    if corruption == "project":
        page = replace(page, project_id=uuid4())
    elif corruption == "run":
        page = replace(page, run_id=uuid4())
    elif corruption == "result":
        page = replace(page, result_id=uuid4())
    elif corruption in {"item_run", "item_result"}:
        item = replace(items[0], run_id=uuid4()) if corruption == "item_run" else replace(
            items[0], result_id=uuid4(),
        )
        page = replace(page, items=(item, items[1]))
    elif corruption == "duplicate":
        page = replace(page, items=(items[0], items[0]))
    elif corruption == "reverse":
        page = replace(page, items=tuple(reversed(items)))
    elif corruption == "cursor":
        page = replace(page, next_cursor=uuid4())
    elif corruption == "oversize":
        params["limit"] = 1
    elif corruption == "short_cursor_page":
        page = replace(page, items=(items[0],), next_cursor=items[0].evaluation_id)
    else:
        params["after"] = str(items[0].evaluation_id)
    monkeypatch.setattr(service, "list_page", AsyncMock(return_value=page))
    response = client.get(f"{base}/evaluations/page", params=params)
    assert response.status_code == 503 and response.json()["code"] == "evaluation_record_invalid"
    assert "items" not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_cancellation_propagates_without_fake_failure_or_resubmission(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """原 task の取消は commit の否定ではなく、別の API Problem や再送を作らない。"""

    _base, body, service = _install(client)
    methods = {
        "legacy_post": "create", "legacy_get": "list_for_run", "submit": "submit",
        "confirm": "get_submission", "page": "list_page",
    }
    monkeypatch.setattr(service, methods[operation], AsyncMock(side_effect=asyncio.CancelledError))
    app = cast(FastAPI, client.app)
    request = Request({"type": "http", "app": app, "headers": [],
                       "state": {"request_id": str(uuid4())}})
    assert service.scope is not None
    project_id, run_id = service.scope
    actor = app.state.auth_service.actor
    with pytest.raises(asyncio.CancelledError):
        if operation == "legacy_post":
            await routes.create_evaluation(
                request, project_id, run_id,
                routes.CreateEvaluationRequest(rating=4, verdict=EvaluationVerdict.ACCURATE), actor,
            )
        elif operation == "legacy_get":
            await routes.list_evaluations(request, project_id, run_id, actor)
        elif operation == "submit":
            await routes.submit_evaluation(
                request, Response(), project_id, run_id,
                routes.SubmitEvaluationRequest.model_validate(body), actor,
            )
        elif operation == "confirm":
            await routes.get_evaluation_submission(request, project_id, run_id,
                                                   UUID(body["submission_key"]), service.result_id,
                                                   actor)
        else:
            await routes.list_evaluation_page(request, project_id, run_id, actor)
    assert service.items == []
