"""MCP 工具の Schema 宣言と metadata に保存できない資格本文を区別する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from skillmind.integrations.domain import (
    CreateIntegrationCommand,
    IntegrationValidationError,
    normalize_integration_command,
)
from skillmind.integrations.mcp_schema import validate_schema, validate_value
from skillmind.integrations.mcp_tools import find_sensitive_tool_metadata
from tests.agent.test_mcp_tools import config
from tests.integrations.test_readonly_resources import command


def login_command() -> CreateIntegrationCommand:
    """サーバー内の資格でログインし、要求にはコントロール ID だけを渡す合成契約。"""
    settings = config()
    settings["tool_catalog"]["tools"].append(
        {
            "name": "login",
            "description": "Use the server-configured test account.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "requestId": {"type": "string", "format": "uuid"},
                    "appId": {"type": "string", "minLength": 1},
                    "windowId": {"type": "string", "format": "uuid"},
                    "usernameAutomationId": {"type": "string", "minLength": 1},
                    "passwordAutomationId": {"type": "string"},
                    "loginButtonAutomationId": {"type": "string", "minLength": 1},
                    "timeoutSeconds": {"type": "integer", "minimum": 1, "default": 15},
                },
                "required": [
                    "requestId", "appId", "windowId", "usernameAutomationId",
                    "passwordAutomationId", "loginButtonAutomationId",
                ],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {"loginConfigured": {"type": "boolean"}},
            },
            "read_only_hint": False,
        }
    )
    settings["tool_permissions"]["login"] = "call"
    return replace(
        command("mcp"),
        config=settings,
        secret_reference_id=uuid4(),
        capabilities=("mcp.tools/v1", "mcp.query/v1", "mcp.call/v1"),
        scope={"resource_uris": [], "tool_names": sorted(settings["tool_permissions"])},
    )


def test_catalog_declarations_save_unchanged_and_validate_actual_control_references() -> None:
    """保存で Schema を削らず、同じ凍結定義で実引数を確認できる。"""
    value = login_command()
    before = deepcopy(value.config)
    saved = normalize_integration_command(value)
    tool = next(item for item in saved.config["tool_catalog"]["tools"] if item["name"] == "login")
    validate_value(
        tool["input_schema"],
        {
            "requestId": str(uuid4()), "appId": "sample", "windowId": str(uuid4()),
            "usernameAutomationId": "username-input", "passwordAutomationId": "login-input",
            "loginButtonAutomationId": "login-button",
        },
    )
    assert value.config == before
    assert tool == next(item for item in before["tool_catalog"]["tools"] if item["name"] == "login")
    assert saved.secret_reference_id == value.secret_reference_id
    assert saved.scope == value.scope


@pytest.mark.parametrize("schema_field", ["input_schema", "output_schema"])
@pytest.mark.parametrize("field", ["password", "accessToken", "clientSecret"])
def test_schema_credential_field_names_are_declarations(schema_field: str, field: str) -> None:
    """汎用 MCP の入力・出力契約では、資格を含み得る項目名の宣言自体を許可する。"""
    metadata = {schema_field: {"type": "object", "properties": {field: {"type": "string"}}}}
    assert find_sensitive_tool_metadata(metadata) is None


@pytest.mark.parametrize("keyword", ["default", "const", "enum", "examples"])
@pytest.mark.parametrize("use_reference", [False, True])
def test_schema_cannot_embed_secret_literals(keyword: str, use_reference: bool) -> None:
    """直接宣言と局所参照のどちらでも、秘密 field の初期値や例を保存しない。"""
    literal = ["synthetic-only"] if keyword in {"enum", "examples"} else "synthetic-only"
    child = {"type": "string", keyword: literal}
    schema: dict[str, Any] = {"type": "object", "properties": {"password": child}}
    if use_reference:
        schema["$defs"] = {"value": child}
        schema["properties"]["password"] = {"$ref": "#/$defs/value"}
    with pytest.raises(ValueError, match="credential-like literal"):
        validate_schema(schema)
    assert find_sensitive_tool_metadata({"input_schema": schema}) == "password"


@pytest.mark.parametrize(
    "child",
    [
        {"anyOf": [{"type": "string", "default": "synthetic-only"}]},
        {"type": "array", "items": {"type": "string", "examples": ["synthetic-only"]}},
    ],
)
def test_nested_secret_literals_keep_the_property_context(child: dict[str, Any]) -> None:
    """合成 Schema や配列へ進んでも元の秘密項目名を見失わない。"""
    with pytest.raises(ValueError, match="credential-like literal"):
        validate_schema({"type": "object", "properties": {"password": child}})


def test_regular_defaults_and_control_reference_defaults_remain_supported() -> None:
    """普通の業務値とコントロール参照は、Schema の既存機能を狭めない。"""
    schema = {
        "type": "object",
        "properties": {
            "passwordAutomationId": {"type": "string", "default": "login-input"},
            "status": {"type": "string", "enum": ["READY", "DONE"], "default": "READY"},
        },
    }
    validate_schema(schema)
    assert find_sensitive_tool_metadata({"input_schema": schema}) is None


@pytest.mark.parametrize("keyword", ["default", "const", "enum", "examples"])
def test_schema_object_literals_do_not_receive_a_schema_exception(keyword: str) -> None:
    """object 値は Schema 宣言ではなく、入れ子の資格名も共有検出へ渡す。"""
    value = {"nested": {"password": "synthetic-only"}}
    schema = {"type": "object", keyword: [value] if keyword in {"enum", "examples"} else value}
    with pytest.raises(ValueError, match="credential-like literal"):
        validate_schema(schema)


@pytest.mark.parametrize("placement", ["config", "scope", "schema_default"])
def test_integration_still_rejects_inline_credentials(placement: str) -> None:
    """接続本体・権限・工具初期値の資格を、Schema 除外で通過させない。"""
    value = login_command()
    if placement == "config":
        value.config["password"] = "synthetic-only"
    elif placement == "scope":
        value.scope["password"] = "synthetic-only"
    else:
        tool = value.config["tool_catalog"]["tools"][-1]
        tool["input_schema"]["properties"]["password"] = {
            "type": "string", "default": "synthetic-only"
        }
    with pytest.raises(IntegrationValidationError, match="credential-like field"):
        normalize_integration_command(value)


def test_invalid_schema_is_not_exempted_from_sensitive_key_checks() -> None:
    """外部参照を持つ未検証契約には、項目名の例外を与えない。"""
    schema = {"properties": {"password": {"$ref": "https://example.test/schema"}}}
    assert find_sensitive_tool_metadata({"input_schema": schema}) == "password"
