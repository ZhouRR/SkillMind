"""MCP の Schema 宣言は一覧だけで許可し、実値の秘密保護と監査を維持する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolDefinition,
    ToolGatewayError,
    ToolRegistry,
    _reject_sensitive_response_keys,
)
from skillmind.integrations.mcp_tools import digest
from tests.agent.test_tool_gateway import (
    CsvIssueProvider,
    MemoryAuditWriter,
    _context,
    _registry,
    _schema,
)
from tests.integrations.test_mcp_metadata import login_command


class CatalogProvider:
    """ネットワークなしで実 MCP 一覧契約と Evidence を Gateway へ渡す。"""

    def __init__(self, *, invalid: bool) -> None:
        """秘密 literal 混入の切替と、replay による二重取得防止の観測を保持する。"""
        self.invalid = invalid
        self.calls = 0
        catalog = login_command().config["tool_catalog"]
        self.response = {
            "status": "success",
            "provider": "mcp",
            "profile": "mcp-tools/v1",
            "tools": [
                {**item, "access": "call" if item["name"] == "login" else "read"}
                for item in catalog["tools"]
            ],
            "server": catalog["server"],
            "catalog_hash": digest(catalog),
            "connection_ref": str(uuid4()),
            "binding_ref": str(uuid4()),
            "desktop": None,
            "limits": {"tool_call_timeout_seconds": 90},
            "observed_at": datetime.now(UTC).isoformat(),
        }

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """同じ Schema を返し、失敗時は本番の資格 guard に判定を任せる。"""
        self.calls += 1
        response = deepcopy(self.response)
        if self.invalid:
            tool = next(item for item in response["tools"] if item["name"] == "login")
            tool["input_schema"]["properties"]["password"] = {"default": "synthetic-only"}
        return ProviderToolResult(
            response=response,
            evidence=(
                EvidenceDraft(
                    evidence_type="resource",
                    source_uri="mcp://integration/fixture/tools",
                    source_locator={"catalog_hash": response["catalog_hash"]},
                    content_hash=digest(response),
                ),
            ),
        )


@pytest.mark.parametrize("invalid", [False, True])
async def test_mcp_catalog_is_validated_before_audit_and_on_replay(
    tmp_path: Path, invalid: bool
) -> None:
    """正規 Schema をそのまま発行・再利用し、資格を含む契約は Evidence 確定前に拒否する。"""
    provider = CatalogProvider(invalid=invalid)
    registry = ToolRegistry(
        (
            ToolDefinition(
                capability="mcp.tools/v1",
                description="Discover authorized tools",
                request_schema=_schema("tools/mcp.tools/v1/request.schema.json"),
                response_schema=_schema("tools/mcp.tools/v1/response.schema.json"),
                error_schema=_schema("tools/mcp.tools/v1/error.schema.json"),
                providers={"mcp": provider},
            ),
        )
    )
    context = replace(
        _context(tmp_path, _registry(CsvIssueProvider())),
        tools=(registry.resolve("mcp.tools/v1", provider="mcp", integration_id=uuid4()),),
        permission_snapshot={"mode": "auto_read_only", "allowed_capabilities": ["mcp.tools/v1"]},
    )
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    sdk_name, session_id = context.tools[0].sdk_name, str(uuid4())
    assert runtime.mcp.on_tool_authorized is not None
    await runtime.mcp.on_tool_authorized(sdk_name, {}, "catalog-1", session_id)
    result = await runtime.gateway.invoke_mcp(sdk_name, {})
    payload = json.loads(result["content"][0]["text"])
    if invalid:
        assert result["is_error"] is True
        assert payload["code"] == "unavailable"
        assert "synthetic-only" not in json.dumps(result)
        assert not writer.completed and writer.failed
        return
    assert payload["status"] == "success"
    assert payload["tools"] == provider.response["tools"]
    assert payload["evidence_refs"] and len(writer.completed) == 1
    await runtime.mcp.on_tool_authorized(sdk_name, {}, "catalog-1", session_id)
    replay = await runtime.gateway.invoke_mcp(sdk_name, {})
    assert replay == result and provider.calls == 1


def test_schema_exemption_does_not_apply_to_tool_results() -> None:
    """任意の読取結果が input_schema を名乗っても資格検査を迂回できない。"""
    tool = login_command().config["tool_catalog"]["tools"][-1]
    with pytest.raises(ToolGatewayError, match="sensitive field"):
        _reject_sensitive_response_keys({"result": tool}, capability="mcp.query/v1")
    with pytest.raises(ToolGatewayError, match="sensitive field"):
        _reject_sensitive_response_keys(
            {"result": {"password": "synthetic-only"}}, capability="mcp.tools/v1"
        )


def test_evidence_allows_control_references_without_allowing_credentials() -> None:
    """実呼出しの locator は保存でき、password 本文を保存できるようにはしない。"""
    data = {"arguments": {"passwordAutomationId": "login-input"}}
    evidence = EvidenceDraft(
        evidence_type="resource",
        source_uri="mcp://integration/fixture/tools",
        source_locator=data,
        content_hash=digest(data),
    )
    assert evidence.source_locator == data
    with pytest.raises(ValueError, match="sensitive field"):
        replace(evidence, source_locator={"arguments": {"password": "synthetic-only"}})
