"""Run-scoped ToolRegistry と in-process MCP Gateway を実装する。"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import UUID, uuid4

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.domain import RegisteredTool, RunContext, RunWorkspace
from skillmind.agent.engine import RunMcpRuntime
from skillmind.agent.evidence import (
    EvidenceDraft,
    EvidenceRecord,
    ToolAuditLease,
    ToolAuditWriter,
    ToolInvocation,
    invocation_fingerprint,
    new_evidence_ref,
    validate_artifact_publication,
)
from skillmind.agent.tool_policy import ToolExecutionPolicy, capability_to_sdk_name
from skillmind.core.hashing import canonical_json
from skillmind.core.redaction import find_sensitive_key

_MCP_TOOL_PREFIX = "mcp__skillmind__"
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
    # 子 context の派生元であり、実行権そのものではない。frozen dataclass の入れ子は可変なので、
    # 権限縮小は共有 resolver、監査の提交権は Worker-private scope と DB lease で検証する。
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
        """検証済み引数で登録された scope 内の Provider を実行する。"""

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

        # Hook caller の入れ子を await 前に切り離し、許可した内容と fingerprint を固定する。
        arguments = deepcopy(dict(arguments))
        registered = self._policy.authorize(tool_name, arguments)
        invocation = self._invocation(
            registered, arguments, tool_use_id=tool_use_id, session_id=session_id
        )
        lease = await self._audit_writer.start_authorized(invocation)
        key = (tool_name, invocation.request_fingerprint)
        async with self._lock:
            self._pending[key].append(deepcopy(lease))

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
                    "Tool invocation was not registered by Skillmind",
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
            tool=deepcopy(registered),
            arguments=deepcopy(dict(arguments)),
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
        self._dispatched: set[UUID] = set()

    async def invoke_mcp(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """MCP SDK が要求する content/is_error 形式へ結果を変換する。"""

        binding = self._bindings[tool_name]
        try:
            response = await self._invoke(binding, deepcopy(dict(arguments)))
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
        """初回の実行権だけを消費し、既存の未決/失敗を再実行しない。"""

        lease = await self._coordinator.claim(binding.registered.sdk_name, arguments)
        replay = lease.status == "SUCCEEDED" and lease.result is not None
        replay_result = deepcopy(lease.result) if replay else None
        if not replay:
            if (
                lease.status != "RUNNING"
                or not lease.is_new
                or lease.tool_call_id in self._dispatched
            ):
                raise ToolGatewayError(
                    "unavailable", "Tool invocation cannot be executed again", retryable=False
                )
            # 次の await より先に一度だけ消費する。確認が失敗しても自動的に実行権を返さない。
            self._dispatched.add(lease.tool_call_id)
        try:
            # PreToolUse の許可と MCP 到着は別時点。新規と成功再読取の両方で現在の原権限を確認する。
            await self._audit_writer.verify_dispatch(lease)
        except Exception as error:
            raise ToolGatewayError(
                "unavailable", "Tool execution authority could not be verified", retryable=False
            ) from error
        if replay:
            assert replay_result is not None
            return self._checked_response(binding, replay_result)
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
            if "evidence_refs" in result.response or "artifact_refs" in result.response:
                raise ToolGatewayError(
                    "unavailable",
                    "Provider must not assign platform references",
                    retryable=False,
                )
            records = tuple(
                EvidenceRecord(
                    evidence_ref=new_evidence_ref(),
                    draft=replace(
                        draft,
                        source_locator=deepcopy(dict(draft.source_locator)),
                        metadata=deepcopy(dict(draft.metadata or {})),
                        artifact=replace(draft.artifact) if draft.artifact else None,
                    ),
                    artifact_ref=f"art_{uuid4().hex}" if draft.artifact else None,
                )
                for draft in result.evidence
            )
            response = deepcopy(dict(result.response))
            response["evidence_refs"] = [record.evidence_ref for record in records]
            if binding.registered.capability == "workspace.write/v2":
                response["artifact_refs"] = [
                    record.artifact_ref for record in records if record.artifact_ref is not None
                ]
            if binding.registered.capability == "workspace.write/v2" or any(
                record.artifact_ref is not None for record in records
            ):
                if lease.invocation is None:
                    raise ValueError("Artifact publication requires the original invocation")
                validate_artifact_publication(lease.invocation, result=response, evidence=records)
            response = self._checked_response(binding, response)
        except ToolProviderError as error:
            await self._record_failure(
                lease,
                code=error.code,
                retryable=error.retryable,
                duration_ms=_duration_ms(started),
            )
            raise ToolGatewayError(error.code, error.message, retryable=error.retryable) from None
        except ToolGatewayError as error:
            await self._record_failure(
                lease,
                code=error.code,
                retryable=error.retryable,
                duration_ms=_duration_ms(started),
            )
            raise
        except Exception as error:
            await self._record_failure(
                lease,
                code="unavailable",
                retryable=False,
                duration_ms=_duration_ms(started),
            )
            raise ToolGatewayError(
                "unavailable", "Tool execution failed", retryable=False
            ) from error
        try:
            return await self._audit_writer.complete(
                lease,
                result=response,
                evidence=records,
                duration_ms=_duration_ms(started),
            )
        except Exception as error:
            # commit 応答喪失は rollback の証拠ではない。fail 更新や Provider 再実行をせず、
            # 次の原 invocation 照会で保存済み成功か未決かを判定する。
            raise ToolGatewayError(
                "unavailable", "Tool result audit could not be confirmed", retryable=False
            ) from error

    def _checked_response(
        self, binding: _ResolvedBinding, response: Mapping[str, Any]
    ) -> dict[str, Any]:
        """新規/保存済み応答を同じ契約と出力上限で検証し、元の JSON を変更しない。"""

        candidate = deepcopy(dict(response))
        _reject_sensitive_response_keys(candidate)
        _validate_response(binding.definition.response_schema, candidate)
        try:
            size = len(_compact_json(candidate).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError) as error:
            raise ToolGatewayError(
                "unavailable", "Tool response is not valid JSON", retryable=False
            ) from error
        if size > self._context.limits.max_output_bytes:
            raise ToolGatewayError("too_large", "Tool response is too large", retryable=False)
        return candidate

    async def _record_failure(
        self, lease: ToolAuditLease, *, code: str, retryable: bool, duration_ms: int
    ) -> None:
        """失敗監査も原実行権に従い、DB 詳細を公開 Tool 応答へ出さない。"""

        try:
            await self._audit_writer.fail(
                lease, code=code, retryable=retryable, duration_ms=duration_ms
            )
        except Exception as error:
            raise ToolGatewayError(
                "unavailable", "Tool failure audit could not be confirmed", retryable=False
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
        server = create_sdk_mcp_server("skillmind", version="0.1.0", tools=sdk_tools)
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
