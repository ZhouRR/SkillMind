"""ToolCall、PermissionDecision、Evidence の追加式 audit writer を実装する。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.agent.domain import RegisteredTool
from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.core.redaction import find_sensitive_key
from projectmind.db.models import Evidence, PermissionDecision, ToolCall

_CONTENT_HASH_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """PreToolUse で確定した Run 境界と SDK invocation identity。"""

    run_id: UUID
    run_attempt_id: UUID
    agent_session_id: UUID
    sdk_tool_use_id: str
    tool: RegisteredTool
    arguments: Mapping[str, Any]
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ToolAuditLease:
    """MCP handler が引き継ぐ永続 ToolCall と既存結果。"""

    tool_call_id: UUID
    status: str
    result: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EvidenceDraft:
    """Provider が返す、資格情報を含まない Evidence の定位情報。"""

    evidence_type: str
    source_uri: str
    source_locator: Mapping[str, Any]
    content_hash: str
    snapshot_uri: str | None = None
    excerpt: str | None = None
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        """Hash、URI、excerpt、JSON safety を永続化前に検証する。"""

        if not self.evidence_type or len(self.evidence_type) > 64:
            raise ValueError("Evidence type is invalid")
        if _CONTENT_HASH_PATTERN.fullmatch(self.content_hash) is None:
            raise ValueError("Evidence content hash must use sha256:<hex>")
        _validate_safe_uri(self.source_uri, name="source_uri")
        if self.snapshot_uri is not None:
            _validate_safe_uri(self.snapshot_uri, name="snapshot_uri")
        if self.excerpt is not None and len(self.excerpt) > 2_000:
            raise ValueError("Evidence excerpt is too large")
        _json_copy(dict(self.source_locator))
        _json_copy(dict(self.metadata or {}))
        _reject_sensitive_keys(self.source_locator)
        _reject_sensitive_keys(self.metadata or {})


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Gateway が採番した公開 Evidence reference と保存内容。"""

    evidence_ref: str
    draft: EvidenceDraft


class ToolAuditWriter(Protocol):
    """Tool Gateway と database transaction を分離する audit port。"""

    async def start_authorized(self, invocation: ToolInvocation) -> ToolAuditLease:
        """AUTO_ALLOW を追加し、ToolCall を RUNNING にする。"""

        ...

    async def record_denied(self, invocation: ToolInvocation, *, reason: str) -> None:
        """境界違反を DENY decision と ToolCall として追加する。"""

        ...

    async def complete(
        self,
        lease: ToolAuditLease,
        *,
        result: dict[str, Any],
        evidence: tuple[EvidenceRecord, ...],
        duration_ms: int,
    ) -> dict[str, Any]:
        """Evidence と成功結果を同じ transaction で確定する。"""

        ...

    async def fail(
        self,
        lease: ToolAuditLease,
        *,
        code: str,
        retryable: bool,
        duration_ms: int,
    ) -> None:
        """Provider や検証失敗を分類だけ保存する。"""

        ...


class PostgresToolAuditWriter:
    """PostgreSQL transaction で ToolCall と Evidence の整合性を保証する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Transaction ごとの session factory を保持する。"""

        self._session_factory = session_factory

    async def start_authorized(self, invocation: ToolInvocation) -> ToolAuditLease:
        """同一 SDK tool_use_id を idempotent に開始し、AUTO_ALLOW を保存する。"""

        async with self._session_factory() as session, session.begin():
            existing = await self._find(session, invocation, lock=True)
            if existing is not None:
                _verify_fingerprint(existing, invocation)
                return ToolAuditLease(
                    existing.id,
                    existing.status,
                    _copy_optional(existing.result_json),
                )

            now = datetime.now(UTC)
            tool_call = _new_tool_call(invocation, status="RUNNING", now=now)
            session.add(tool_call)
            await session.flush()
            session.add(
                PermissionDecision(
                    id=uuid4(),
                    run_id=invocation.run_id,
                    tool_call_id=tool_call.id,
                    policy="auto_read_only",
                    decision="AUTO_ALLOW",
                    decided_by=None,
                    reason="Registered ProjectMind read-only tool",
                    request_fingerprint=invocation.request_fingerprint,
                    request_json=_arguments_summary(invocation.arguments),
                    decided_at=now,
                )
            )
            return ToolAuditLease(tool_call.id, tool_call.status)

    async def record_denied(self, invocation: ToolInvocation, *, reason: str) -> None:
        """拒否理由を固定長に制限し、入力値そのものは保存しない。"""

        async with self._session_factory() as session, session.begin():
            existing = await self._find(session, invocation, lock=True)
            if existing is not None:
                _verify_fingerprint(existing, invocation)
                return
            now = datetime.now(UTC)
            tool_call = _new_tool_call(invocation, status="DENIED", now=now)
            session.add(tool_call)
            await session.flush()
            session.add(
                PermissionDecision(
                    id=uuid4(),
                    run_id=invocation.run_id,
                    tool_call_id=tool_call.id,
                    policy="auto_read_only",
                    decision="DENY",
                    decided_by=None,
                    reason=reason[:500],
                    request_fingerprint=invocation.request_fingerprint,
                    request_json=_arguments_summary(invocation.arguments),
                    decided_at=now,
                )
            )

    async def complete(
        self,
        lease: ToolAuditLease,
        *,
        result: dict[str, Any],
        evidence: tuple[EvidenceRecord, ...],
        duration_ms: int,
    ) -> dict[str, Any]:
        """再実行済みなら既存結果を返し、初回だけ Evidence を追加する。"""

        async with self._session_factory() as session, session.begin():
            tool_call = await session.get(ToolCall, lease.tool_call_id, with_for_update=True)
            if tool_call is None:
                raise LookupError("ToolCall disappeared before completion")
            if tool_call.status == "SUCCEEDED" and tool_call.result_json is not None:
                return dict(tool_call.result_json)
            if tool_call.status != "RUNNING":
                raise RuntimeError("ToolCall is not running")
            now = datetime.now(UTC)
            session.add_all(
                [
                    Evidence(
                        id=uuid4(),
                        evidence_ref=record.evidence_ref,
                        run_id=tool_call.run_id,
                        tool_call_id=tool_call.id,
                        evidence_type=record.draft.evidence_type,
                        source_uri=record.draft.source_uri,
                        source_locator=dict(record.draft.source_locator),
                        content_hash=record.draft.content_hash,
                        snapshot_uri=record.draft.snapshot_uri,
                        excerpt=record.draft.excerpt,
                        metadata_json=dict(record.draft.metadata or {}),
                        created_at=now,
                    )
                    for record in evidence
                ]
            )
            tool_call.status = "SUCCEEDED"
            tool_call.duration_ms = duration_ms
            tool_call.result_json = _json_copy(result)
            tool_call.error_json = None
            return dict(result)

    async def fail(
        self,
        lease: ToolAuditLease,
        *,
        code: str,
        retryable: bool,
        duration_ms: int,
    ) -> None:
        """成功済み呼び出しを上書きせず、安定分類だけを保存する。"""

        async with self._session_factory() as session, session.begin():
            tool_call = await session.get(ToolCall, lease.tool_call_id, with_for_update=True)
            if tool_call is None:
                raise LookupError("ToolCall disappeared before failure update")
            if tool_call.status == "SUCCEEDED":
                return
            tool_call.status = "FAILED"
            tool_call.duration_ms = duration_ms
            tool_call.error_json = {"code": code, "retryable": retryable}

    async def _find(
        self, session: AsyncSession, invocation: ToolInvocation, *, lock: bool
    ) -> ToolCall | None:
        """Run と SDK tool_use_id の unique key で ToolCall を取得する。"""

        statement = select(ToolCall).where(
            ToolCall.run_id == invocation.run_id,
            ToolCall.sdk_tool_use_id == invocation.sdk_tool_use_id,
        )
        if lock:
            statement = statement.with_for_update()
        return (await session.scalars(statement)).one_or_none()


def invocation_fingerprint(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """Tool 名と正規化引数から coordinator/audit 共通 fingerprint を返す。"""

    return sha256_hex(canonical_json({"tool_name": tool_name, "arguments": dict(arguments)}))


def new_evidence_ref() -> str:
    """Contract の ev_ pattern に適合する衝突しにくい reference を採番する。"""

    return f"ev_{uuid4().hex}"


def _new_tool_call(invocation: ToolInvocation, *, status: str, now: datetime) -> ToolCall:
    """Invocation から secret を含まない ToolCall model を構築する。"""

    return ToolCall(
        id=uuid4(),
        run_id=invocation.run_id,
        run_attempt_id=invocation.run_attempt_id,
        agent_session_id=invocation.agent_session_id,
        sdk_tool_use_id=invocation.sdk_tool_use_id,
        request_fingerprint=invocation.request_fingerprint,
        tool_name=invocation.tool.sdk_name,
        capability_version=invocation.tool.capability,
        provider=invocation.tool.provider,
        integration_id=invocation.tool.integration_id,
        arguments_summary=_arguments_summary(invocation.arguments),
        status=status,
        duration_ms=None,
        result_json=None,
        error_json=None,
        created_at=now,
        updated_at=now,
    )


def _arguments_summary(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """引数値を保存せず、key と任意 purpose の hash だけを記録する。"""

    summary: dict[str, Any] = {"keys": sorted(str(key) for key in arguments)}
    purpose = arguments.get("purpose")
    if isinstance(purpose, str):
        summary["purpose_hash"] = sha256_hex(purpose)
    return summary


def _verify_fingerprint(tool_call: ToolCall, invocation: ToolInvocation) -> None:
    """同じ SDK ID が別 request に再利用された場合は実行を拒否する。"""

    if tool_call.request_fingerprint != invocation.request_fingerprint:
        raise ValueError("SDK tool use ID was reused with different arguments")


def _validate_safe_uri(value: str, *, name: str) -> None:
    """Credential や query を埋め込めない論理 source URI だけを許可する。"""

    if not value or len(value) > 2_048 or any(ord(character) < 32 for character in value):
        raise ValueError(f"Evidence {name} is invalid")
    parsed = urlsplit(value)
    if not parsed.scheme or parsed.username or parsed.password or parsed.query:
        raise ValueError(f"Evidence {name} must not contain credentials or query parameters")


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    """NaN や非 JSON 値を拒否し、caller の mutable object から切り離す。"""

    try:
        loaded = json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError("Tool audit data must be JSON-safe") from error
    if not isinstance(loaded, dict):
        raise TypeError("Tool audit data must be a JSON object")
    return loaded


def _copy_optional(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Nullable JSON result を defensive copy する。"""

    return None if value is None else dict(value)


def _reject_sensitive_keys(value: Any) -> None:
    """Evidence metadata へ credential らしい field が混入することを拒否する。"""

    if find_sensitive_key(value) is not None:
        raise ValueError("Evidence metadata contains a sensitive field")
