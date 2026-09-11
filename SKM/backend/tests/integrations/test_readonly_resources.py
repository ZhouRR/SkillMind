"""PostgreSQL と MCP の接続登録、Secret と読取範囲の契約を検証する。"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from skillmind.integrations.domain import (
    CreateIntegrationCommand,
    IntegrationValidationError,
    normalize_integration_command,
)


def command(provider: str) -> CreateIntegrationCommand:
    """外部接続しない合成 Provider 設定を作る。"""
    postgres = provider == "postgres"
    return CreateIntegrationCommand(
        project_id=uuid4(),
        name="Read-only reports",
        kind="other",
        provider=provider,
        capabilities=("database.read/v1" if postgres else "mcp.read/v1",),
        scope={"tables": ["public.reports"]}
        if postgres
        else {"resource_uris": ["resource://reports/current"]},
        config={
            "host": "db.example.test",
            "port": 5432,
            "database": "reports",
            "username": "reader",
            "sslmode": "verify-full",
        }
        if postgres
        else {"server_url": "https://mcp.example.test/mcp", "transport": "streamable_http"},
        secret_reference_id=uuid4() if postgres else None,
        created_by=uuid4(),
    )


@pytest.mark.parametrize("provider", ["postgres", "mcp"])
def test_resource_connection_and_explicit_scope_are_preserved(provider: str) -> None:
    """非機密 metadata と明示範囲を canonical command に保存する。"""
    original = command(provider)
    assert normalize_integration_command(original) == original


def test_mcp_scope_stores_sdk_canonical_uris_without_duplicates() -> None:
    """URI は登録時に一度だけ正規化し、SDK が実行時に別の URI を選ぶ余地をなくす。"""
    original = replace(
        command("mcp"),
        scope={"resource_uris": ["https://REPORTS.example.test", "https://reports.example.test/"]},
    )
    normalized = normalize_integration_command(original)
    assert normalized.scope == {"resource_uris": ["https://reports.example.test/"]}


def test_mcp_canonical_unicode_scope_can_be_normalized_again() -> None:
    """percent encoding で長くなった URI も、作成と後の binding 検証で同じ値になる。"""
    original = replace(command("mcp"), scope={"resource_uris": ["resource://reports/" + "文" * 80]})
    normalized = normalize_integration_command(original)
    assert len(normalized.scope["resource_uris"][0]) > 256
    assert normalize_integration_command(normalized) == normalized


@pytest.mark.parametrize("provider", ["postgres", "mcp"])
@pytest.mark.parametrize("values", [[], ["*"], ["invalid"], [123]])
def test_resource_scope_rejects_missing_wildcard_and_malformed_values(
    provider: str,
    values: list[object],
) -> None:
    """空・全許可・不正な識別子を登録で拒否する。"""
    key = "tables" if provider == "postgres" else "resource_uris"
    with pytest.raises(IntegrationValidationError):
        normalize_integration_command(replace(command(provider), scope={key: values}))


@pytest.mark.parametrize("provider", ["postgres", "mcp"])
def test_resource_rejects_write_capabilities_and_inline_credentials(provider: str) -> None:
    """読取接続の宣言から write や平文 config へ広げられない。"""
    original = command(provider)
    with pytest.raises(IntegrationValidationError):
        normalize_integration_command(replace(original, capabilities=("repository.write/v1",)))
    with pytest.raises(IntegrationValidationError):
        normalize_integration_command(
            replace(original, config={**original.config, "password": "fixture"})
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("host", "postgres://user@db.example.test"),
        ("port", True),
        ("port", 0),
        ("port", 65536),
        ("port", "5432"),
        ("sslmode", "prefer"),
        ("database", ""),
    ],
)
def test_postgres_connection_validation(key: str, value: object) -> None:
    """曖昧な接続先・型・TLS 設定を永続化しない。"""
    original = command("postgres")
    with pytest.raises(IntegrationValidationError):
        normalize_integration_command(replace(original, config={**original.config, key: value}))


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:fixture@mcp.example.test/mcp",
        "https://mcp.example.test/mcp?key=fixture",
        "https://mcp.example.test/mcp#fragment",
    ],
)
def test_mcp_connection_rejects_credential_urls_and_non_http_targets(url: str) -> None:
    """接続 URL は HTTP(S) の endpoint だけを保持する。"""
    original = command("mcp")
    with pytest.raises(IntegrationValidationError):
        normalize_integration_command(
            replace(original, config={**original.config, "server_url": url})
        )
