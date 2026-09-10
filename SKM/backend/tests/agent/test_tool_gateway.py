"""Run-scoped ToolRegistry、MCP Gateway、Evidence 先行保存を検証する。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from skillmind.agent.domain import RunContext, RunLimits, RunWorkspace
from skillmind.agent.evidence import (
    EvidenceDraft,
    EvidenceRecord,
    ToolAuditLease,
    ToolInvocation,
)
from skillmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolDefinition,
    ToolProviderError,
    ToolRegistry,
)

ROOT = Path(__file__).resolve().parents[3]


def _schema(relative: str) -> dict[str, Any]:
    """Repository の versioned Tool Schema を読み込む。"""

    value = json.loads((ROOT / "contracts" / relative).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _content_hash(value: str) -> str:
    """Evidence fixture 用 SHA-256 reference を返す。"""

    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


class CsvIssueProvider:
    """Credential を持たない issue.read conformance fixture。"""

    def __init__(self, *, invalid_response: bool = False) -> None:
        """Invalid response test の切替と呼出回数を保持する。"""

        self.invalid_response = invalid_response
        self.calls = 0
        self.contexts: list[RunToolContext] = []

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """固定 Ticket と再現可能な CSV Evidence を返す。"""

        self.calls += 1
        self.contexts.append(context)
        response: dict[str, Any] = {
            "status": "success",
            "provider": "csv",
            "issue": {
                "id": arguments["issue_ref"],
                "subject": "fixture",
                "description": None,
                "status": "open",
                "updated_at": None,
                "fields": {},
                "extensions": {},
            },
            "warnings": [],
            "truncated": False,
        }
        if self.invalid_response:
            del response["issue"]
        return ProviderToolResult(
            response=response,
            evidence=(
                EvidenceDraft(
                    evidence_type="issue",
                    source_uri="csv://integration-fixture/issues/TICKET-1",
                    source_locator={"sheet": "issues", "row": 2},
                    content_hash=_content_hash("fixture-row"),
                    excerpt="fixture",
                    metadata={"reproducibility": "snapshot"},
                ),
            ),
        )


class MemoryAuditWriter:
    """Gateway transaction order と idempotency を観測する in-memory port。"""

    def __init__(self) -> None:
        """Tool use ID ごとの lease と監査結果を初期化する。"""

        self.by_use_id: dict[str, ToolAuditLease] = {}
        self.invocations: dict[UUID, ToolInvocation] = {}
        self.denied: list[tuple[ToolInvocation, str]] = []
        self.completed: list[tuple[UUID, tuple[EvidenceRecord, ...]]] = []
        self.failed: list[tuple[UUID, str, bool]] = []

    async def start_authorized(self, invocation: ToolInvocation) -> ToolAuditLease:
        """同じ SDK ID の成功結果を retry 時に再利用する。"""

        existing = self.by_use_id.get(invocation.sdk_tool_use_id)
        if existing is not None:
            return replace(existing, is_new=False)
        lease = ToolAuditLease(uuid4(), "RUNNING", invocation=invocation, is_new=True)
        self.by_use_id[invocation.sdk_tool_use_id] = lease
        self.invocations[lease.tool_call_id] = invocation
        return lease

    async def record_denied(self, invocation: ToolInvocation, *, reason: str) -> None:
        """Denied invocation と理由を記録する。"""

        self.denied.append((invocation, reason))

    async def verify_dispatch(self, lease: ToolAuditLease) -> None:
        """Gateway の実行前 port を満たす。実 DB lease は別の transaction 回帰が検証する。"""

        assert lease.tool_call_id in self.invocations

    async def complete(
        self,
        lease: ToolAuditLease,
        *,
        result: dict[str, Any],
        evidence: tuple[EvidenceRecord, ...],
        duration_ms: int,
    ) -> dict[str, Any]:
        """Evidence がある場合だけ成功結果を cache する。"""

        assert evidence
        assert duration_ms >= 0
        completed = ToolAuditLease(
            lease.tool_call_id, "SUCCEEDED", deepcopy(result), invocation=lease.invocation
        )
        invocation = self.invocations[lease.tool_call_id]
        self.by_use_id[invocation.sdk_tool_use_id] = completed
        self.completed.append((lease.tool_call_id, evidence))
        return result

    async def fail(
        self,
        lease: ToolAuditLease,
        *,
        code: str,
        retryable: bool,
        duration_ms: int,
    ) -> None:
        """公開 error 分類だけを記録する。"""

        assert duration_ms >= 0
        self.failed.append((lease.tool_call_id, code, retryable))


def _registry(provider: CsvIssueProvider) -> ToolRegistry:
    """実 contract と CSV fixture を結ぶ ToolRegistry を返す。"""

    return ToolRegistry(
        (
            ToolDefinition(
                capability="issue.read/v1",
                description="Read one issue from the bound project source",
                request_schema=_schema("tools/issue.read/v1/request.schema.json"),
                response_schema=_schema("tools/issue.read/v1/response.schema.json"),
                error_schema=_schema("tools/issue.read/v1/error.schema.json"),
                providers={"csv": provider},
            ),
        )
    )


def _context(tmp_path: Path, registry: ToolRegistry) -> RunContext:
    """一つの issue.read binding だけを含む RunContext を返す。"""

    run_id = uuid4()
    root = tmp_path / str(run_id)
    registered = registry.resolve("issue.read/v1", provider="csv", integration_id=uuid4())
    return RunContext(
        run_id=run_id,
        run_attempt_id=uuid4(),
        project_id=uuid4(),
        user_id=uuid4(),
        prompt="analyze",
        task_snapshot={},
        skill_snapshots=(),
        resolved_sources={"issue_source": "csv"},
        permission_snapshot={
            "mode": "auto_read_only",
            "allowed_capabilities": ["issue.read/v1"],
        },
        workspace=RunWorkspace(
            root=root,
            cwd=root / "workspace",
            input_dir=root / "input",
            output_dir=root / "output",
            temp_dir=root / "temp",
        ),
        limits=RunLimits(max_turns=10, wall_timeout_seconds=60, max_output_bytes=20_000),
        result_schema={"type": "object"},
        tools=(registered,),
        model="claude-test",
    )


@pytest.mark.asyncio
async def test_gateway_persists_evidence_before_return_and_reuses_success(
    tmp_path: Path,
) -> None:
    """成功 response は Evidence ref を含み、同じ SDK call は Provider を再実行しない。"""

    provider = CsvIssueProvider()
    registry = _registry(provider)
    context = _context(tmp_path, registry)
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    arguments = {"issue_ref": "TICKET-1", "purpose": "analysis"}
    session_id = str(uuid4())
    assert runtime.mcp.on_tool_authorized is not None

    await runtime.mcp.on_tool_authorized(
        context.tools[0].sdk_name, arguments, "tool-use-1", session_id
    )
    first = await runtime.gateway.invoke_mcp(context.tools[0].sdk_name, arguments)
    await runtime.mcp.on_tool_authorized(
        context.tools[0].sdk_name, arguments, "tool-use-1", session_id
    )
    second = await runtime.gateway.invoke_mcp(context.tools[0].sdk_name, arguments)

    first_payload = json.loads(first["content"][0]["text"])
    second_payload = json.loads(second["content"][0]["text"])
    assert first_payload == second_payload
    assert first_payload["evidence_refs"][0].startswith("ev_")
    assert provider.calls == 1
    assert len(writer.completed) == 1
    assert provider.contexts[0].tool.integration_id == context.tools[0].integration_id


@pytest.mark.asyncio
async def test_gateway_rejects_unregistered_mcp_invocation(tmp_path: Path) -> None:
    """PreToolUse 登録を経ない handler 呼び出しは Provider へ到達しない。"""

    provider = CsvIssueProvider()
    registry = _registry(provider)
    context = _context(tmp_path, registry)
    runtime = registry.build_gateway_runtime(context, audit_writer=MemoryAuditWriter())

    result = await runtime.gateway.invoke_mcp(
        context.tools[0].sdk_name,
        {"issue_ref": "TICKET-1", "purpose": "analysis"},
    )

    payload = json.loads(result["content"][0]["text"])
    assert result["is_error"] is True
    assert payload["code"] == "invalid_request"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_pre_tool_denial_is_audited_without_argument_values(tmp_path: Path) -> None:
    """境界変更引数を hard deny し、Tool の実値を監査へ複製しない。"""

    provider = CsvIssueProvider()
    registry = _registry(provider)
    context = _context(tmp_path, registry)
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    assert runtime.mcp.on_tool_denied is not None
    arguments = {
        "issue_ref": "SECRET-TICKET",
        "purpose": "analysis",
        "project_id": str(uuid4()),
    }

    await runtime.mcp.on_tool_denied(
        context.tools[0].sdk_name,
        arguments,
        "tool-use-denied",
        str(uuid4()),
        "Tool input attempts to change run boundary: project_id",
    )

    assert len(writer.denied) == 1
    invocation, reason = writer.denied[0]
    assert invocation.tool.capability == "issue.read/v1"
    assert "project_id" in reason
    assert "SECRET-TICKET" not in reason


@pytest.mark.asyncio
async def test_invalid_provider_response_fails_before_evidence_commit(tmp_path: Path) -> None:
    """Contract 不適合 response は Evidence を作らず stable error へ変換する。"""

    provider = CsvIssueProvider(invalid_response=True)
    registry = _registry(provider)
    context = _context(tmp_path, registry)
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    arguments = {"issue_ref": "TICKET-1", "purpose": "analysis"}
    assert runtime.mcp.on_tool_authorized is not None
    await runtime.mcp.on_tool_authorized(
        context.tools[0].sdk_name, arguments, "tool-use-invalid", str(uuid4())
    )

    result = await runtime.gateway.invoke_mcp(context.tools[0].sdk_name, arguments)
    payload = json.loads(result["content"][0]["text"])

    assert result["is_error"] is True
    assert payload["code"] == "unavailable"
    assert writer.completed == []
    assert writer.failed[0][1] == "unavailable"


def test_evidence_rejects_credentials_and_invalid_hash() -> None:
    """Evidence URI への credential 混入と非 SHA-256 hash を拒否する。"""

    with pytest.raises(ValueError, match="credentials"):
        EvidenceDraft(
            evidence_type="issue",
            source_uri="https://user:secret@example.test/issues/1",
            source_locator={},
            content_hash=_content_hash("x"),
        )
    with pytest.raises(ValueError, match="sha256"):
        EvidenceDraft(
            evidence_type="issue",
            source_uri="csv://fixture/issues/1",
            source_locator={},
            content_hash="md5:bad",
        )
    with pytest.raises(ValueError, match="sensitive field"):
        EvidenceDraft(
            evidence_type="issue",
            source_uri="csv://fixture/issues/1",
            source_locator={"sheet": "issues"},
            content_hash=_content_hash("x"),
            metadata={"access_token": "must-not-be-stored"},
        )


def test_provider_error_keeps_stable_public_fields() -> None:
    """Provider error は公開 code、message、retryable だけを保持する。"""

    error = ToolProviderError("unavailable", "Source temporarily unavailable", retryable=True)

    assert error.code == "unavailable"
    assert error.message == "Source temporarily unavailable"
    assert error.retryable is True
