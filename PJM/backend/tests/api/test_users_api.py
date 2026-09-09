"""アカウント管理 HTTP の公開面と、domain 拒否を非秘密 Problem にする境界。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fakes import FakeAuthService, FakeUserService
from fastapi.testclient import TestClient
from httpx import Response
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.auth.login_protection import LoginProtectionUnavailableError, LoginRateLimitedError
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.users.domain import (
    CurrentPasswordRejectedError,
    LastActiveAdminError,
    UserAdministrationDeniedError,
    UserEmailConflictError,
    UserNotFoundError,
    UserRole,
    UserStatus,
    UserVersionConflictError,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
BASE = "/api/v1/users"
NEW_PASSWORD = "test-only replacement password"
ACCOUNT_OPERATIONS = (
    ("GET", "/me/account", None),
    ("GET", "/me/security-events", None),
    ("POST", "/me/password", "password-request"),
    ("POST", "/me/sessions/revoke", "version-request"),
    ("GET", "", None),
    ("POST", "", "create-request"),
    ("GET", "/{user_id}", None),
    ("PUT", "/{user_id}", "update-request"),
    ("POST", "/{user_id}/sessions/revoke", "version-request"),
    ("GET", "/{user_id}/security-events", None),
)


@pytest.fixture
def account_api(client: TestClient) -> tuple[FakeAuthService, FakeUserService]:
    """Lifespan の装配が実際に存在することを確認した後、DB だけを fake にする。"""

    from projectmind.users.service import UserService

    assert isinstance(client.app.state.user_service, UserService)
    auth = client.app.state.auth_service
    service = FakeUserService(auth.actor)
    client.app.state.user_service = service
    client.cookies.set(client.app.state.settings.auth_session_cookie_name, auth.session_token)
    return auth, service


def validate_contract(name: str, value: object) -> None:
    """Response model と独立した versioned Schema で実 JSON を検査する。"""

    schema = json.loads((CONTRACTS / f"users/v1/{name}.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)


def assert_declared_response(client: TestClient, response: Response, path: str) -> None:
    """全十操作の実 status/media type/cache/UUID と条件付き cookie を OpenAPI に照合する。"""

    operation = client.app.openapi()["paths"][BASE + path][response.request.method.lower()]
    declared = operation["responses"][str(response.status_code)]
    assert response.headers["content-type"] in declared["content"]
    assert UUID(response.headers["x-request-id"]).version == 4
    for name, header in declared["headers"].items():
        if header.get("required"):
            assert name in response.headers
        if name in response.headers:
            value: str | int = response.headers[name]
            if header["schema"].get("type") == "integer":
                assert isinstance(value, str) and value.isdecimal()
                value = int(value)
            Draft202012Validator(header["schema"]).validate(value)
    if response.status_code >= 400:
        schema = declared["content"]["application/problem+json"]["schema"]
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(response.json())
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert "set-cookie" not in response.headers
    else:
        assert ("set-cookie" in response.headers) is response.json().get("session_revoked", False)


def request_body(contract: str | None) -> dict[str, object] | None:
    """匿名認証や権限の拒否を本文 validation の失敗で誤認しないよう有効な fixture を使う。"""

    if contract is None:
        return None
    return json.loads((CONTRACTS / "examples" / f"user-{contract}.v1.json").read_text())


@pytest.mark.parametrize("method,path,contract", ACCOUNT_OPERATIONS)
def test_every_account_operation_rejects_an_expired_actor_before_the_use_case(
    client: TestClient,
    account_api: tuple[FakeAuthService, FakeUserService],
    method: str,
    path: str,
    contract: str | None,
) -> None:
    """Session 失効は Project の有無によらず十操作全てで 401 として止める。"""

    auth, service = account_api
    auth.unauthorized = True
    response = client.request(
        method, BASE + path.replace("{user_id}", str(uuid4())), json=request_body(contract)
    )
    assert response.status_code == 401 and response.json()["code"] == "authentication_required"
    assert_declared_response(client, response, path)
    assert not service.calls and not auth.password_change_actors


@pytest.mark.parametrize("method,path,contract", ACCOUNT_OPERATIONS)
def test_user_role_can_use_only_own_account_operations(
    client: TestClient,
    account_api: tuple[FakeAuthService, FakeUserService],
    method: str,
    path: str,
    contract: str | None,
) -> None:
    """本人四操作は USER に開き、本人 ID を管理 URL に入れても ADMIN 制約を保つ。"""

    auth, service = account_api
    auth.actor = replace(auth.actor, system_role="USER")
    response = client.request(
        method,
        BASE + path.replace("{user_id}", str(auth.actor.user_id)),
        json=request_body(contract),
    )
    assert_declared_response(client, response, path)
    if path.startswith("/me/"):
        assert response.status_code == 200 and len(service.calls) == 1
        assert service.calls[0][1]["access"].actor.system_role == "USER"
    else:
        assert response.status_code == 403 and response.json()["code"] == "administrator_required"
        assert not service.calls


@pytest.mark.parametrize(
    "method,path,contract", [operation for operation in ACCOUNT_OPERATIONS if operation[2]]
)
@pytest.mark.parametrize(
    "boundary", ["missing-origin", "foreign-origin", "missing-csrf", "wrong-csrf"]
)
def test_every_account_mutation_enforces_origin_and_session_csrf(
    client: TestClient,
    account_api: tuple[FakeAuthService, FakeUserService],
    method: str,
    path: str,
    contract: str,
    boundary: str,
) -> None:
    """入力が正しくても五つの書込入口は Origin/CSRF の欠落や不一致を先に拒否する。"""

    auth, service = account_api
    if boundary == "missing-origin":
        del client.headers["Origin"]
    elif boundary == "foreign-origin":
        client.headers["Origin"] = "https://untrusted.example.test"
    elif boundary == "missing-csrf":
        del client.headers["X-CSRF-Token"]
    else:
        client.headers["X-CSRF-Token"] = "wrong-test-only-csrf"
    response = client.request(
        method, BASE + path.replace("{user_id}", str(uuid4())), json=request_body(contract)
    )
    assert response.status_code == (422 if boundary == "missing-csrf" else 403)
    assert_declared_response(client, response, path)
    assert not service.calls and not auth.password_change_actors


def test_admin_reads_exact_user_without_searching_a_list_page(
    client: TestClient, account_api: tuple[FakeAuthService, FakeUserService]
) -> None:
    """編集の再読込は精確 ID と原会話を渡し、追加 field や cache を許さない。"""

    auth, service = account_api
    target = uuid4()
    response = client.get(BASE + f"/{target}")
    assert response.status_code == 200
    assert_declared_response(client, response, "/{user_id}")
    validate_contract("account", response.json())
    assert response.json()["user_id"] == str(target)
    assert response.headers["cache-control"] == "no-store"
    operation, arguments = service.calls[0]
    assert operation == "get_user" and arguments["user_id"] == target
    assert arguments["access"].session_token == auth.session_token
    assert arguments["access"].request_id == UUID(response.headers["x-request-id"])


def test_admin_user_read_hides_missing_target_and_internal_detail(
    client: TestClient, account_api: tuple[FakeAuthService, FakeUserService]
) -> None:
    """不存在と別組織の target を同じ no-store Problem として扱う。"""

    _, service = account_api
    service.failure = UserNotFoundError("internal target detail")
    response = client.get(BASE + f"/{uuid4()}")
    assert response.status_code == 404 and response.json()["code"] == "user_not_found"
    assert "internal target detail" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_account_lists_and_audit_use_explicit_projection(
    client: TestClient, account_api: tuple[FakeAuthService, FakeUserService]
) -> None:
    """本人/管理一覧、検索/page、監査を同じ信頼された actor に接続する。"""

    auth, service = account_api
    for path, contract in (
        ("/me/account", "account"),
        ("?q=reader&limit=25&offset=100", "list"),
        ("/me/security-events", "security-events"),
        (f"/{uuid4()}/security-events?offset=25", "security-events"),
    ):
        response = client.get(BASE + path, headers={"X-Request-ID": "untrusted-caller-marker"})
        assert response.status_code == 200
        operation_path = path.split("?")[0]
        if operation_path.endswith("/security-events") and not operation_path.startswith("/me/"):
            operation_path = "/{user_id}/security-events"
        assert_declared_response(client, response, operation_path)
        validate_contract(contract, response.json())
        assert response.headers["cache-control"] == "no-store"
        request_id = UUID(response.headers["x-request-id"])
        assert request_id.version == 4
        access = service.calls[-1][1]["access"]
        assert access.actor.user_id == auth.actor.user_id
        assert access.request_id == request_id and access.session_token == auth.session_token
        assert "untrusted-caller-marker" not in response.text
        assert (
            "password" not in response.text
            and "csrf" not in response.text
            and "token_hash" not in response.text
        )
    assert service.calls[1][1]["query"] == "reader"
    assert service.calls[1][1]["offset"] == 100
    assert service.calls[2][1]["user_id"] == auth.actor.user_id


@pytest.mark.parametrize(
    "operation", ["create", "update", "password", "self-revoke", "admin-revoke"]
)
def test_mutations_forward_version_and_clear_cookie_only_for_current_revocation(
    client: TestClient, account_api: tuple[FakeAuthService, FakeUserService], operation: str
) -> None:
    """実 service に判定を委ね、現在会話の失効結果がある場合だけ cookie を削除する。"""

    auth, service = account_api
    service.revoked = operation in {"password", "self-revoke"}
    target = uuid4()
    routes = {
        "create": (
            "POST",
            "",
            {
                "email": "new@example.test",
                "display_name": "New",
                "system_role": "USER",
                "password": NEW_PASSWORD,
            },
        ),
        "update": (
            "PUT",
            f"/{target}",
            {
                "display_name": "New",
                "system_role": "USER",
                "status": "DISABLED",
                "expected_row_version": 1,
            },
        ),
        "password": (
            "POST",
            "/me/password",
            {
                "current_password": "test-only current",
                "new_password": NEW_PASSWORD,
                "expected_row_version": 1,
            },
        ),
        "self-revoke": ("POST", "/me/sessions/revoke", {"expected_row_version": 1}),
        "admin-revoke": ("POST", f"/{target}/sessions/revoke", {"expected_row_version": 1}),
    }
    method, suffix, body = routes[operation]
    contract = {
        "create": "create-request",
        "update": "update-request",
        "password": "password-request",
    }.get(operation, "version-request")
    validate_contract(contract, body)
    response = client.request(method, BASE + suffix, json=body)
    assert response.status_code == (201 if operation == "create" else 200)
    assert_declared_response(client, response, suffix.replace(str(target), "{user_id}"))
    validate_contract("mutation", response.json())
    assert response.headers["cache-control"] == "no-store"
    assert ("Max-Age=0" in response.headers.get("set-cookie", "")) == service.revoked
    if service.revoked:
        assert (
            "HttpOnly" in response.headers["set-cookie"]
            and "SameSite=strict" in response.headers["set-cookie"]
        )
    assert NEW_PASSWORD not in response.text
    assert service.calls[-1][1]["access"].csrf_token == auth.csrf_token
    if operation == "update":
        assert service.calls[-1][1]["command"].expected_row_version == body["expected_row_version"]
    elif operation != "create":
        assert service.calls[-1][1]["expected_row_version"] == body["expected_row_version"]
    if operation == "password":
        assert auth.password_change_actors == [auth.actor.user_id]
        assert auth.login_sources == ["testclient"]
    elif operation == "self-revoke":
        assert service.calls[-1][1]["user_id"] == auth.actor.user_id
    elif operation == "admin-revoke":
        assert service.calls[-1][1]["user_id"] == target


@pytest.mark.parametrize(
    "role,status", [(UserRole.USER, UserStatus.ACTIVE), (UserRole.ADMIN, UserStatus.DISABLED)]
)
def test_admin_self_update_forwards_session_revocation_and_deletes_current_cookie(
    client: TestClient,
    account_api: tuple[FakeAuthService, FakeUserService],
    role: UserRole,
    status: UserStatus,
) -> None:
    """管理 URL の本人変更にも同じ cookie 契約を適用する。DB の末位 ADMIN 判定は模倣しない。"""

    auth, service = account_api
    service.account = replace(service.account, system_role=role, status=status)
    service.revoked = True
    response = client.put(
        BASE + f"/{auth.actor.user_id}",
        json={
            "display_name": service.account.display_name,
            "system_role": role,
            "status": status,
            "expected_row_version": 1,
        },
    )
    assert response.status_code == 200
    assert_declared_response(client, response, "/{user_id}")
    validate_contract("mutation", response.json())
    result = response.json()
    assert result["session_revoked"] is True and result["revoked_sessions"] == 1
    assert result["user"]["user_id"] == str(auth.actor.user_id)
    assert result["user"]["system_role"] == role and result["user"]["status"] == status
    assert result["user"]["row_version"] == 2
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(client.app.state.settings.auth_session_cookie_name + "=")
    assert all(part in cookie for part in ("Max-Age=0", "Path=/", "HttpOnly", "SameSite=strict"))
    assert ("Secure" in cookie) is client.app.state.settings.auth_cookie_secure
    assert "Domain=" not in cookie
    operation, arguments = service.calls[0]
    assert operation == "update" and arguments["user_id"] == auth.actor.user_id
    assert arguments["access"].actor.system_role == "ADMIN"
    assert not auth.logged_out


def test_rejected_last_admin_self_update_does_not_delete_cookie(
    client: TestClient, account_api: tuple[FakeAuthService, FakeUserService]
) -> None:
    """Service が最後の ADMIN の変更を拒否した場合は、自己失効の成功応答を捏造しない。"""

    auth, service = account_api
    service.failure = LastActiveAdminError("test-only last administrator")
    response = client.put(
        BASE + f"/{auth.actor.user_id}",
        json={
            "display_name": service.account.display_name,
            "system_role": "USER",
            "status": "ACTIVE",
            "expected_row_version": 1,
        },
    )
    assert response.status_code == 409 and response.json()["code"] == "last_active_admin"
    assert_declared_response(client, response, "/{user_id}")
    assert service.account.row_version == 1 and not auth.logged_out


@pytest.mark.parametrize(
    "error,status,code",
    [
        (UnauthorizedSessionError, 401, "authentication_required"),
        (CsrfRejectedError, 403, "csrf_rejected"),
        (UserAdministrationDeniedError, 403, "administrator_required"),
        (UserNotFoundError, 404, "user_not_found"),
        (UserVersionConflictError, 409, "user_version_conflict"),
        (UserEmailConflictError, 409, "user_email_conflict"),
        (LastActiveAdminError, 409, "last_active_admin"),
        (ValueError, 422, "invalid_user_request"),
    ],
)
def test_domain_rejections_are_declared_and_redacted(
    client: TestClient,
    account_api: tuple[FakeAuthService, FakeUserService],
    error: type[Exception],
    status: int,
    code: str,
) -> None:
    """内部の理由や秘密を公開せず、同じ OpenAPI operation の Problem と一致させる。"""

    _, service = account_api
    service.failure = error("hidden-sensitive-value")
    caller_id = str(uuid4())
    response = client.put(
        BASE + f"/{uuid4()}",
        headers={"X-Request-ID": caller_id},
        json={
            "display_name": "New",
            "status": "ACTIVE",
            "system_role": "USER",
            "expected_row_version": 1,
        },
    )
    assert response.status_code == status and response.json()["code"] == code
    assert "hidden-sensitive-value" not in response.text
    assert response.json()["request_id"] == response.headers["x-request-id"] != caller_id
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    declared = client.app.openapi()["paths"][BASE + "/{user_id}"]["put"]["responses"][str(status)]
    Draft202012Validator(declared["content"]["application/problem+json"]["schema"]).validate(
        response.json()
    )


def test_current_password_rejection_does_not_expire_the_session(
    client: TestClient, account_api: tuple[FakeAuthService, FakeUserService]
) -> None:
    """改密の password 誤りは 400 であり、401 logout や cookie 削除にしない。"""

    _, service = account_api
    service.failure = CurrentPasswordRejectedError("hidden")
    response = client.post(
        BASE + "/me/password",
        json={
            "current_password": "incorrect",
            "new_password": NEW_PASSWORD,
            "expected_row_version": 1,
        },
    )
    assert response.status_code == 400 and response.json()["code"] == "current_password_rejected"
    assert_declared_response(client, response, "/me/password")
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("source", [True, False])
@pytest.mark.parametrize("unavailable", [True, False])
def test_password_quotas_stop_before_management(
    client: TestClient,
    account_api: tuple[FakeAuthService, FakeUserService],
    source: bool,
    unavailable: bool,
) -> None:
    """Body 前と actor 配額の両段階で拒否し、管理 transaction を起動しない。"""

    auth, service = account_api
    error = LoginProtectionUnavailableError("hidden") if unavailable else LoginRateLimitedError(30)
    setattr(
        auth, "begin_login" if source else "admit_password_change", AsyncMock(side_effect=error)
    )
    response = client.post(
        BASE + "/me/password",
        json={}
        if source
        else {
            "current_password": "current",
            "new_password": NEW_PASSWORD,
            "expected_row_version": 1,
        },
    )
    assert response.status_code == (503 if unavailable else 429)
    assert_declared_response(client, response, "/me/password")
    assert response.headers["cache-control"] == "no-store"
    if not unavailable:
        assert response.headers["retry-after"] == "30"
    else:
        assert "retry-after" not in response.headers
    assert not service.calls


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_row_version": True},
        {"expected_row_version": "1"},
        {"expected_row_version": 0},
        {"expected_row_version": 1, "password_hash": "hidden-value"},
    ],
)
def test_unsafe_validation_rejects_coercion_and_extra_secret_fields(
    client: TestClient,
    account_api: tuple[FakeAuthService, FakeUserService],
    payload: dict[str, object],
) -> None:
    """版を曖昧に変換せず、validation error へ入力値を含めない。"""

    _, service = account_api
    response = client.post(BASE + "/me/sessions/revoke", json=payload)
    assert response.status_code == 422 and not service.calls
    assert "hidden-value" not in response.text and response.headers["cache-control"] == "no-store"


def test_user_cannot_administer_others_and_origin_is_required(
    client: TestClient, account_api: tuple[FakeAuthService, FakeUserService]
) -> None:
    """隠したボタンではなく actor dependency が管理と CSRF を拒否する。"""

    auth, service = account_api
    auth.actor = replace(auth.actor, system_role="USER")
    for method, path in (
        ("GET", ""),
        ("GET", f"/{uuid4()}"),
        ("GET", f"/{auth.actor.user_id}"),
        ("GET", f"/{uuid4()}/security-events"),
        ("POST", f"/{uuid4()}/sessions/revoke"),
    ):
        response = client.request(
            method, BASE + path, json={"expected_row_version": 1} if method == "POST" else None
        )
        assert response.status_code == 403 and response.json()["code"] == "administrator_required"
    response = client.post(
        BASE + "/me/sessions/revoke",
        headers={"Origin": "https://untrusted.example"},
        json={"expected_row_version": 1},
    )
    assert response.status_code == 403 and response.json()["code"] == "csrf_rejected"
    assert not service.calls
