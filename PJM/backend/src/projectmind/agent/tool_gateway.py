"""Run-scoped ToolRegistry と in-process MCP Gateway を実装する。"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.agent.domain import RegisteredTool, RunContext, RunWorkspace
from projectmind.agent.engine import RunMcpRuntime
from projectmind.agent.evidence import (
    EvidenceDraft,
    EvidenceRecord,
    ToolAuditLease,
    ToolAuditWriter,
    ToolInvocation,
    invocation_fingerprint,
    new_evidence_ref,
)
from projectmind.agent.tool_policy import ToolExecutionPolicy, capability_to_sdk_name
from projectmind.core.hashing import canonical_json
from projectmind.core.redaction import find_sensitive_key

_MCP_TOOL_PREFIX = "mcp__projectmind__"
_EXECUTION_PROFILE_ORDER = ("GUIDED", "SUPERVISED", "DELEGATED")


@dataclass(frozen=True, slots=True)
class RunToolContext:
    """Provider handler が利用できる、モデルから変更不能な Run 境界。"""

    run_id: UUID
    run_attempt_id: UUID
    project_id: UUID
    user_id: UUID
    tool: RegisteredTool
    workspace: RunWorkspace
    # 凍結された Run snapshot 本体。扇出 Provider が受限の子 context を派生させるために要る
    # (計画 §23 D7)。Provider は frozen dataclass しか受け取らないため、ここから権限や上限を
    # 広げることはできない——`replace()` で狭める方向にしか使えない。
    run: RunContext | None = None


@dataclass(frozen=True, slots=True)
class ProviderToolResult:
    """Provider response と、返却前に保存すべき Evidence。"""

    response: Mapping[str, Any]
    evidence: tuple[EvidenceDraft, ...]


class ToolProvider(Protocol):
    """Integration credential を Gateway 内に閉じ込める Provider port。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """検証済み引数で read-only Provider を実行する。"""

        ...


class ToolProviderError(RuntimeError):
    """Agent へ返してよい安定 code と message を持つ Provider error。"""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        """資格情報を含まない公開 error を保持する。"""

        if not message or len(message) > 500 or any(ord(character) < 32 for character in message):
            raise ValueError("Tool Provider error message is invalid")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """一つの versioned capability と利用可能 Provider の定義。"""

    capability: str
    description: str
    request_schema: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    error_schema: Mapping[str, Any]
    providers: Mapping[str, ToolProvider]
    # Integration/resource binding を要しない platform-owned Provider だけが指定できる。
    unbound_provider: str | None = None
    minimum_execution_profile: str = "GUIDED"
    # True の control Tool は PreToolUse で SDK を停止し、Provider handler へ到達させない。
    defer_execution: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedBinding:
    """Run snapshot の RegisteredTool と実装済み Provider の対応。"""

    registered: RegisteredTool
    definition: ToolDefinition
    provider: ToolProvider


class ToolGatewayError(RuntimeError):
    """MCP の is_error response へ変換する安全な Tool error。"""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        """契約に出力する安定分類を保持する。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class ToolInvocationCoordinator:
    """PreToolUse の tool_use_id と MCP handler 呼び出しを fingerprint で結ぶ。"""

    def __init__(
        self,
        context: RunContext,
        policy: ToolExecutionPolicy,
        audit_writer: ToolAuditWriter,
    ) -> None:
        """Run identity、hard policy、audit writer を保持する。"""

        self._context = context
        self._policy = policy
        self._audit_writer = audit_writer
        self._pending: dict[tuple[str, str], deque[ToolAuditLease]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def register_authorized(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        tool_use_id: str,
        session_id: str,
    ) -> None:
        """PreToolUse で再検証し、AUTO_ALLOW 後に FIFO queue へ登録する。"""

        registered = self._policy.authorize(tool_name, arguments)
        invocation = self._invocation(
            registered, arguments, tool_use_id=tool_use_id, session_id=session_id
        )
        lease = await self._audit_writer.start_authorized(invocation)
        key = (tool_name, invocation.request_fingerprint)
        async with self._lock:
            self._pending[key].append(lease)

    async def register_denied(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        tool_use_id: str,
        session_id: str,
        reason: str,
    ) -> None:
        """未登録 Tool を含む hard deny を入力値なしで監査する。"""

        registered = self._policy.registered(tool_name) or RegisteredTool(
            capability="platform.denied/v1",
            sdk_name=tool_name[:128] or "unknown",
            provider="platform",
            integration_id=None,
            input_schema={"type": "object"},
        )
        invocation = self._invocation(
            registered, arguments, tool_use_id=tool_use_id, session_id=session_id
        )
        await self._audit_writer.record_denied(invocation, reason=reason)

    async def claim(self, tool_name: str, arguments: Mapping[str, Any]) -> ToolAuditLease:
        """MCP handler を直前の PreToolUse audit lease と対応付ける。"""

        fingerprint = invocation_fingerprint(tool_name, arguments)
        key = (tool_name, fingerprint)
        async with self._lock:
            queue = self._pending.get(key)
            if not queue:
                raise ToolGatewayError(
                    "invalid_request",
                    "Tool invocation was not registered by ProjectMind",
                    retryable=False,
                )
            lease = queue.popleft()
            if not queue:
                del self._pending[key]
            return lease

    def _invocation(
        self,
        registered: RegisteredTool,
        arguments: Mapping[str, Any],
        *,
        tool_use_id: str,
        session_id: str,
    ) -> ToolInvocation:
        """Hook identity を UUID と fingerprint へ正規化する。"""

        return ToolInvocation(
            run_id=self._context.run_id,
            run_attempt_id=self._context.run_attempt_id,
            agent_session_id=UUID(session_id),
            sdk_tool_use_id=tool_use_id,
            tool=registered,
            arguments=dict(arguments),
            request_fingerprint=invocation_fingerprint(registered.sdk_name, arguments),
        )


class ToolGateway:
    """Provider 実行、response 検証、Evidence 先行保存を所有する。"""

    def __init__(
        self,
        context: RunContext,
        bindings: Mapping[str, _ResolvedBinding],
        coordinator: ToolInvocationCoordinator,
        audit_writer: ToolAuditWriter,
    ) -> None:
        """Run-scoped binding と response 上限を保持する。"""

        self._context = context
        self._bindings = dict(bindings)
        self._coordinator = coordinator
        self._audit_writer = audit_writer

    async def invoke_mcp(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """MCP SDK が要求する content/is_error 形式へ結果を変換する。"""

        binding = self._bindings[tool_name]
        try:
            response = await self._invoke(binding, arguments)
            return {"content": [{"type": "text", "text": _compact_json(response)}]}
        except ToolGatewayError as error:
            payload = {
                "status": "error",
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            }
            errors = list(
                Draft202012Validator(binding.definition.error_schema).iter_errors(payload)
            )
            if errors:
                payload = {
                    "status": "error",
                    "code": "unavailable",
                    "message": "Tool execution failed",
                    "retryable": False,
                }
            return {
                "content": [{"type": "text", "text": _compact_json(payload)}],
                "is_error": True,
            }

    async def _invoke(
        self, binding: _ResolvedBinding, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """監査 lease を取得し、成功時だけ Evidence と response を確定する。"""

        lease = await self._coordinator.claim(binding.registered.sdk_name, arguments)
        if lease.status == "SUCCEEDED" and lease.result is not None:
            return dict(lease.result)
        started = time.monotonic()
        try:
            result = await binding.provider.execute(
                RunToolContext(
                    run_id=self._context.run_id,
                    run_attempt_id=self._context.run_attempt_id,
                    project_id=self._context.project_id,
                    user_id=self._context.user_id,
                    tool=binding.registered,
                    workspace=self._context.workspace,
                    run=self._context,
                ),
                arguments,
            )
            if not result.evidence:
                raise ToolGatewayError("unavailable", "Tool returned no evidence", retryable=False)
            if "evidence_refs" in result.response:
                raise ToolGatewayError(
                    "unavailable",
                    "Provider must not assign evidence references",
                    retryable=False,
                )
            records = tuple(
                EvidenceRecord(evidence_ref=new_evidence_ref(), draft=draft)
                for draft in result.evidence
            )
            response = dict(result.response)
            response["evidence_refs"] = [record.evidence_ref for record in records]
            _reject_sensitive_response_keys(response)
            _validate_response(binding.definition.response_schema, response)
            if len(_compact_json(response).encode("utf-8")) > self._context.limits.max_output_bytes:
                raise ToolGatewayError("too_large", "Tool response is too large", retryable=False)
            duration_ms = _duration_ms(started)
            return await self._audit_writer.complete(
                lease,
                result=response,
                evidence=records,
                duration_ms=duration_ms,
            )
        except ToolProviderError as error:
            await self._audit_writer.fail(
                lease,
                code=error.code,
                retryable=error.retryable,
                duration_ms=_duration_ms(started),
            )
            raise ToolGatewayError(error.code, error.message, retryable=error.retryable) from None
        except ToolGatewayError as error:
            await self._audit_writer.fail(
                lease,
                code=error.code,
                retryable=error.retryable,
                duration_ms=_duration_ms(started),
            )
            raise
        except Exception as error:
            await self._audit_writer.fail(
                lease,
                code="unavailable",
                retryable=False,
                duration_ms=_duration_ms(started),
            )
            raise ToolGatewayError(
                "unavailable", "Tool execution failed", retryable=False
            ) from error


@dataclass(frozen=True, slots=True)
class RunToolRuntime:
    """Engine 接続用 MCP runtime と直接検証可能な Gateway の組。"""

    mcp: RunMcpRuntime
    gateway: ToolGateway


class ToolRegistry:
    """Versioned capability を Run ごとの最小 MCP server へ解決する。"""

    def __init__(self, definitions: tuple[ToolDefinition, ...]) -> None:
        """Capability、Schema、Provider の重複と欠落を起動時に検証する。"""

        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            capability_to_sdk_name(definition.capability)
            if definition.capability in self._definitions:
                raise ValueError(f"Duplicate Tool capability: {definition.capability}")
            if not definition.providers:
                raise ValueError(f"Tool capability has no Provider: {definition.capability}")
            if (
                definition.unbound_provider is not None
                and definition.unbound_provider not in definition.providers
            ):
                raise ValueError(
                    f"Unbound Tool Provider is not installed: {definition.capability}"
                )
            if definition.minimum_execution_profile not in _EXECUTION_PROFILE_ORDER:
                raise ValueError(
                    f"Tool execution profile is invalid: {definition.capability}"
                )
            Draft202012Validator.check_schema(definition.request_schema)
            Draft202012Validator.check_schema(definition.response_schema)
            Draft202012Validator.check_schema(definition.error_schema)
            self._definitions[definition.capability] = definition

    def resolve(
        self,
        capability: str,
        *,
        provider: str,
        integration_id: UUID | None,
        binding_id: UUID | None = None,
        execution_profile: str = "GUIDED",
    ) -> RegisteredTool:
        """Project binding から AgentEngine 用 RegisteredTool を生成する。"""

        definition = self._definitions.get(capability)
        if definition is None:
            raise LookupError(f"Tool capability is not installed: {capability}")
        if provider not in definition.providers:
            raise LookupError(f"Tool Provider is not installed: {capability}/{provider}")
        _require_execution_profile(definition, execution_profile)
        return RegisteredTool(
            capability=capability,
            sdk_name=capability_to_sdk_name(capability),
            provider=provider,
            integration_id=integration_id,
            input_schema=dict(definition.request_schema),
            binding_id=binding_id,
        )

    def resolve_unbound(
        self, capability: str, *, execution_profile: str
    ) -> RegisteredTool:
        """Platform 所有の Run-scoped Provider を Integration binding 無しで解決する。"""

        definition = self._definitions.get(capability)
        if definition is None:
            raise LookupError(f"Tool capability is not installed: {capability}")
        provider = definition.unbound_provider
        if provider is None:
            raise LookupError(f"Tool capability requires a resource binding: {capability}")
        return self.resolve(
            capability,
            provider=provider,
            integration_id=None,
            binding_id=None,
            execution_profile=execution_profile,
        )

    def build_runtime(self, context: RunContext, *, audit_writer: ToolAuditWriter) -> RunMcpRuntime:
        """Run snapshot に含まれる Tool だけを公開する MCP server を構築する。"""

        return self.build_gateway_runtime(context, audit_writer=audit_writer).mcp

    def build_gateway_runtime(
        self, context: RunContext, *, audit_writer: ToolAuditWriter
    ) -> RunToolRuntime:
        """MCP callback と同じ Gateway を conformance test 用にも返す。"""

        raw_capabilities = context.permission_snapshot.get("allowed_capabilities")
        if not isinstance(raw_capabilities, list) or not all(
            isinstance(capability, str) for capability in raw_capabilities
        ):
            raise ValueError("Permission snapshot must contain allowed_capabilities")
        policy = ToolExecutionPolicy(
            context.tools,
            allowed_capabilities=frozenset(raw_capabilities),
        )
        bindings: dict[str, _ResolvedBinding] = {}
        raw_profile = context.permission_snapshot.get("execution_profile", "GUIDED")
        if not isinstance(raw_profile, str) or raw_profile not in _EXECUTION_PROFILE_ORDER:
            raise ValueError("Permission snapshot contains an invalid execution profile")
        for registered in context.tools:
            definition = self._definitions.get(registered.capability)
            if definition is None:
                raise LookupError(f"Tool capability is not installed: {registered.capability}")
            _require_execution_profile(definition, raw_profile)
            if dict(registered.input_schema) != dict(definition.request_schema):
                raise ValueError(f"Run Tool Schema drift: {registered.capability}")
            provider = definition.providers.get(registered.provider)
            if provider is None:
                raise LookupError(
                    f"Tool Provider is not installed: {registered.capability}/{registered.provider}"
                )
            if registered.sdk_name in bindings:
                # MCP 名は capability から一意に決まる。別 ResourceBinding を黙って上書きすると
                # Agent が監査された scope と異なる Provider を呼ぶため、multiplex contract が
                # 導入されるまでは曖昧な同一 capability を fail closed にする。
                raise ValueError(
                    f"Run resolves multiple bindings for one Tool: {registered.capability}"
                )
            bindings[registered.sdk_name] = _ResolvedBinding(
                registered=registered,
                definition=definition,
                provider=provider,
            )

        coordinator = ToolInvocationCoordinator(context, policy, audit_writer)
        gateway = ToolGateway(context, bindings, coordinator, audit_writer)
        sdk_tools = [_build_sdk_tool(binding, gateway) for binding in bindings.values()]
        server = create_sdk_mcp_server("projectmind", version="0.1.0", tools=sdk_tools)
        return RunToolRuntime(
            mcp=RunMcpRuntime(
                server=server,
                on_tool_authorized=coordinator.register_authorized,
                on_tool_denied=coordinator.register_denied,
                deferred_tool_names=frozenset(
                    registered.sdk_name
                    for registered in context.tools
                    if bindings[registered.sdk_name].definition.defer_execution
                ),
            ),
            gateway=gateway,
        )


def _build_sdk_tool(binding: _ResolvedBinding, gateway: ToolGateway) -> SdkMcpTool[Any]:
    """Full SDK 名から MCP server 内の local tool handler を作成する。"""

    local_name = binding.registered.sdk_name.removeprefix(_MCP_TOOL_PREFIX)

    async def handler(arguments: Any) -> dict[str, Any]:
        """MCP の未型付け引数を JSON object として Gateway へ渡す。"""

        if not isinstance(arguments, Mapping):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": _compact_json(
                            {
                                "status": "error",
                                "code": "invalid_request",
                                "message": "Tool arguments must be an object",
                                "retryable": False,
                            }
                        ),
                    }
                ],
                "is_error": True,
            }
        return await gateway.invoke_mcp(binding.registered.sdk_name, arguments)

    return tool(
        local_name,
        binding.definition.description,
        dict(binding.definition.request_schema),
    )(handler)


def _validate_response(schema: Mapping[str, Any], response: Mapping[str, Any]) -> None:
    """Provider 出力が公開 Tool contract と format を満たすことを確認する。"""

    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(response),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        raise ToolGatewayError(
            "unavailable", "Tool response did not match its contract", retryable=False
        )


def _compact_json(value: Mapping[str, Any]) -> str:
    """MCP text response 用の deterministic JSON を返す。"""

    return canonical_json(dict(value))


def _duration_ms(started: float) -> int:
    """監査用 duration を負値にならない millisecond へ変換する。"""

    return max(0, int((time.monotonic() - started) * 1_000))


def _reject_sensitive_response_keys(value: Any) -> None:
    """Provider response に credential field が混入した場合は Agent へ返さない。"""

    if find_sensitive_key(value) is not None:
        raise ToolGatewayError(
            "unavailable", "Tool response contained a sensitive field", retryable=False
        )


def _require_execution_profile(definition: ToolDefinition, actual: str) -> None:
    """Tool が要求する最低自主度を platform snapshot と比較する。"""

    if actual not in _EXECUTION_PROFILE_ORDER:
        raise ValueError("Execution profile is invalid")
    required_index = _EXECUTION_PROFILE_ORDER.index(definition.minimum_execution_profile)
    if _EXECUTION_PROFILE_ORDER.index(actual) < required_index:
        raise LookupError(
            "Tool capability is unavailable for the execution profile: "
            f"{definition.capability}/{actual}"
        )
