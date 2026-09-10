"""Account の十操作を、実 actor dependency と独立 v1 Schema の両方で守る。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from skillmind.api.auth_dependencies import (
    admin_actor,
    authenticated_actor,
    authenticated_admin_actor,
    csrf_authenticated_actor,
)
from skillmind.api.main import create_app
from skillmind.api.problems import PROBLEM_DETAILS_SCHEMA
from skillmind.api.routes import users

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
BASE = "/api/v1/users"
MODELS = (
    ("account", users.UserAccountResponse),
    ("list", users.UserPageResponse),
    ("security-event", users.UserSecurityEventResponse),
    ("security-events", users.UserSecurityEventPageResponse),
    ("mutation", users.UserMutationResponse),
    ("create-request", users.CreateUserRequest),
    ("update-request", users.UpdateUserRequest),
    ("password-request", users.ChangeOwnPasswordRequest),
    ("version-request", users.UserVersionRequest),
)
OPERATIONS = (
    ("/me/account", "get", authenticated_actor, None, "account", {200, 401, 403, 422}),
    (
        "/me/security-events",
        "get",
        authenticated_actor,
        None,
        "security-events",
        {200, 401, 403, 422},
    ),
    (
        "/me/password",
        "post",
        csrf_authenticated_actor,
        "password-request",
        "mutation",
        {200, 400, 401, 403, 409, 422, 429, 503},
    ),
    (
        "/me/sessions/revoke",
        "post",
        csrf_authenticated_actor,
        "version-request",
        "mutation",
        {200, 401, 403, 409, 422},
    ),
    ("", "get", authenticated_admin_actor, None, "list", {200, 401, 403, 422}),
    ("", "post", admin_actor, "create-request", "mutation", {201, 401, 403, 409, 422}),
    ("/{user_id}", "get", authenticated_admin_actor, None, "account", {200, 401, 403, 404, 422}),
    (
        "/{user_id}",
        "put",
        admin_actor,
        "update-request",
        "mutation",
        {200, 401, 403, 404, 409, 422},
    ),
    (
        "/{user_id}/sessions/revoke",
        "post",
        admin_actor,
        "version-request",
        "mutation",
        {200, 401, 403, 404, 409, 422},
    ),
    (
        "/{user_id}/security-events",
        "get",
        authenticated_admin_actor,
        None,
        "security-events",
        {200, 401, 403, 404, 422},
    ),
)


def normalized_schema(value: Any, root: dict[str, Any]) -> Any:
    """注釈と参照配置だけを除き、required・追加禁止・nullable・値域の差分を残す。"""

    if isinstance(value, list):
        return [normalized_schema(item, root) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        assert reference.startswith("#/")
        resolved: Any = root
        for part in reference[2:].split("/"):
            resolved = resolved[part]
        return normalized_schema(resolved, root)
    result = {
        key: normalized_schema(item, root)
        for key, item in value.items()
        if key not in {"$schema", "$id", "$defs", "title", "description"}
    }
    # Pydantic の password format は表示注釈であり、入力専用 writeOnly は比較を維持する。
    if result.get("format") == "password":
        del result["format"]
    if "required" in result:
        result["required"] = sorted(result["required"])
    return result


def contract(name: str) -> dict[str, Any]:
    """Repository の正本を読むだけで example や model から作り直さない。"""

    return json.loads((CONTRACTS / "users" / "v1" / f"{name}.schema.json").read_text())


@pytest.mark.parametrize("name,model", MODELS)
def test_user_models_match_versioned_schemas(name: str, model: type[BaseModel]) -> None:
    """一つの example で通らない省略・enum・境界値の契約 drift も拒否する。"""

    declared, versioned = model.model_json_schema(), contract(name)
    assert normalized_schema(declared, declared) == normalized_schema(versioned, versioned)


@pytest.mark.parametrize("name", ["create-request", "password-request"])
@pytest.mark.parametrize("password", ["x" * 8, "密" * 8, "🙂" * 8, " secret "])
def test_new_password_minimum_matches_models_and_contracts(name: str, password: str) -> None:
    """作成と本人改密の両入口で 8 文字を受理し、7 文字を拒否する。"""

    if name == "create-request":
        model: type[BaseModel] = users.CreateUserRequest
        field = "password"
        body = {
            "email": "synthetic@example.test", "display_name": "Synthetic",
            "system_role": "USER", field: password,
        }
    else:
        model = users.ChangeOwnPasswordRequest
        field = "new_password"
        body = {"expected_row_version": 1, "current_password": "old", field: password}
    validator = Draft202012Validator(contract(name))
    validator.validate(body)
    assert getattr(model.model_validate(body), field).get_secret_value() == password

    body[field] = password[:-1]
    assert not validator.is_valid(body)
    with pytest.raises(ValidationError):
        model.model_validate(body)


def test_all_ten_user_operations_describe_models_guards_statuses_and_headers() -> None:
    """管理六操作と本人四操作を Project 非依存の共有 actor に接続する。"""

    app = create_app()
    document = app.openapi()
    account_routes = [route for route in users.account_router.routes if isinstance(route, APIRoute)]
    # include_router の内部表現ではなく、公開逆引きで app への装配を確認する。
    public_paths = {
        route.name: str(
            app.url_path_for(
                route.name,
                **{parameter: f"{{{parameter}}}" for parameter in route.param_convertors},
            )
        )
        for route in account_routes
    }
    assert {
        (public_paths[route.name], method.lower())
        for route in account_routes
        for method in route.methods
    } == {(BASE + suffix, method) for suffix, method, *_ in OPERATIONS}
    for suffix, method, actor, request, response, statuses in OPERATIONS:
        path = BASE + suffix
        route = next(
            route
            for route in account_routes
            if public_paths[route.name] == path and method.upper() in route.methods
        )
        assert [dependency.call for dependency in route.dependant.dependencies] == [actor]
        operation = document["paths"][path][method]
        assert set(operation["responses"]) == {str(status) for status in statuses}
        assert set(operation["tags"]) == {"users", "auth"}
        csrf = [
            parameter
            for parameter in operation.get("parameters", [])
            if parameter["name"] == "X-CSRF-Token"
        ]
        if request:
            assert len(csrf) == 1 and csrf[0]["required"] is True and csrf[0]["in"] == "header"
            body = operation["requestBody"]
            assert body["required"] is True and set(body["content"]) == {"application/json"}
            expected = contract(request)
            assert normalized_schema(
                body["content"]["application/json"]["schema"], document
            ) == normalized_schema(expected, expected)
        else:
            assert not csrf and "requestBody" not in operation
        for status, declared in operation["responses"].items():
            headers = declared["headers"]
            assert headers["Cache-Control"] == {
                "required": True,
                "schema": {"type": "string", "const": "no-store"},
            }
            assert headers["X-Request-ID"]["required"] is True
            if int(status) >= 400:
                assert declared["content"] == {
                    "application/problem+json": {"schema": PROBLEM_DETAILS_SCHEMA}
                }
                assert "Set-Cookie" not in headers
            else:
                assert int(status) == (201 if method == "post" and suffix == "" else 200)
                assert set(declared["content"]) == {"application/json"}
                expected = contract(response)
                assert normalized_schema(
                    declared["content"]["application/json"]["schema"], document
                ) == normalized_schema(expected, expected)
                assert ("Set-Cookie" in headers) is bool(request)
                if request:
                    assert headers["Set-Cookie"]["required"] is False
            if status == "429":
                assert headers["Retry-After"] == {
                    "required": True,
                    "schema": {"type": "integer", "minimum": 1, "maximum": 300},
                }
            else:
                assert "Retry-After" not in headers


def test_user_list_and_security_paging_declare_bounded_server_queries() -> None:
    """UI の server search と page を、ページ上の client filter に置き換えさせない。"""

    paths = create_app().openapi()["paths"]
    for suffix in ("", "/me/security-events", "/{user_id}/security-events"):
        parameters = {
            parameter["name"]: parameter for parameter in paths[BASE + suffix]["get"]["parameters"]
        }
        limit, offset = parameters["limit"]["schema"], parameters["offset"]["schema"]
        assert (
            limit["type"] == "integer"
            and limit["minimum"] == 1
            and limit["maximum"] == 100
            and limit["default"] == 25
        )
        assert offset["type"] == "integer" and offset["minimum"] == 0 and offset["default"] == 0
        if suffix == "":
            assert parameters["q"]["schema"]["maxLength"] == 200
        else:
            assert "q" not in parameters
