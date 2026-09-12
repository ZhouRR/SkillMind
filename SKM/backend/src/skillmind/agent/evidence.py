"""ToolCall、PermissionDecision、Evidence の追加式 audit writer を実装する。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.domain import RegisteredTool
from skillmind.artifacts.conversion import conversion_artifact, conversion_artifact_description
from skillmind.artifacts.domain import MAX_RUN_ARTIFACT_BYTES, MAX_RUN_ARTIFACTS, ArtifactDraft
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.redaction import find_sensitive_key
from skillmind.db.models import (
    Evidence,
    PermissionDecision,
    Run,
    RunAttempt,
    RunSegment,
    ToolCall,
)
from skillmind.runs.domain import (
    ClaimedRun,
    LeaseValidationError,
    SessionContinuationMode,
)

if TYPE_CHECKING:
    from skillmind.runs.tool_execution import ToolExecutionGate

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
    invocation: ToolInvocation | None = None
    is_new: bool = False


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
    artifact: ArtifactDraft | None = None

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
        if self.artifact is not None:
            # private bytes は metadata/excerpt と別の経路で保存し、公開 JSON に混入させない。
            artifact = replace(self.artifact)
            if self.content_hash != f"sha256:{sha256_hex(artifact.content)}":
                raise ValueError("Artifact content does not match its Evidence")


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Gateway が採番した公開 Evidence reference と保存内容。"""

    evidence_ref: str
    draft: EvidenceDraft
    artifact_ref: str | None = None


class ToolAuditWriter(Protocol):
    """Tool Gateway と database transaction を分離する audit port。"""

    async def start_authorized(self, invocation: ToolInvocation) -> ToolAuditLease:
        """AUTO_ALLOW を追加し、ToolCall を RUNNING にする。"""

        ...

    async def record_denied(self, invocation: ToolInvocation, *, reason: str) -> None:
        """境界違反を DENY decision と ToolCall として追加する。"""

        ...

    async def verify_dispatch(self, lease: ToolAuditLease) -> None:
        """PreToolUse 後の待機で失った実行権を Provider 呼出し直前に再確認する。"""

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

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, claimed_run: ClaimedRun,
        authority_check: Callable[[], None],
    ) -> None:
        """原 Worker claim を内部だけに保持し、後の callback で別の lease を借用しない。"""

        self._session_factory = session_factory
        if not isinstance(claimed_run, ClaimedRun):
            raise LeaseValidationError("Tool audit requires original Worker authority")
        self._claimed = claimed_run
        self._authority_check = authority_check

    async def start_authorized(self, invocation: ToolInvocation) -> ToolAuditLease:
        """同一 SDK tool_use_id を idempotent に開始し、AUTO_ALLOW を保存する。"""

        invocation = self._snapshot_invocation(invocation)
        async with self._session_factory() as session, session.begin():
            gate, locked = await self._lock_execution(session)
            await self._check_document_readiness(session, locked[0], invocation)
            existing = await self._find(session, invocation, lock=True)
            if existing is not None:
                await self._verify_tool_identity(session, existing, invocation)
                await self._finish(session, gate, locked)
                return ToolAuditLease(
                    existing.id,
                    existing.status,
                    _copy_optional(existing.result_json),
                    invocation=invocation,
                    is_new=False,
                )

            now = await gate.validate(self._claimed, locked)
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
                    reason="Registered Skillmind scoped tool",
                    request_fingerprint=invocation.request_fingerprint,
                    request_json=_arguments_summary(invocation.arguments),
                    decided_at=now,
                )
            )
            await self._finish(session, gate, locked)
            return ToolAuditLease(
                tool_call.id, tool_call.status, invocation=invocation, is_new=True,
            )

    async def record_denied(self, invocation: ToolInvocation, *, reason: str) -> None:
        """拒否理由を固定長に制限し、入力値そのものは保存しない。"""

        invocation = self._snapshot_invocation(invocation)
        async with self._session_factory() as session, session.begin():
            gate, locked = await self._lock_execution(session)
            existing = await self._find(session, invocation, lock=True)
            if existing is not None:
                await self._verify_tool_identity(session, existing, invocation)
                await self._finish(session, gate, locked)
                return
            now = await gate.validate(self._claimed, locked)
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
            await self._finish(session, gate, locked)

    async def verify_dispatch(self, lease: ToolAuditLease) -> None:
        """新規一回の実行または保存成功の読取だけを、原実行の有効期間内で許す。"""

        lease, invocation = self._snapshot_lease(lease)
        async with self._session_factory() as session, session.begin():
            gate, locked = await self._lock_execution(session)
            tool_call = await self._lock_tool(session, lease, invocation)
            await self._check_document_readiness(session, locked[0], invocation)
            if tool_call.status == "SUCCEEDED":
                if lease.status != "SUCCEEDED" or (
                    _copy_optional(tool_call.result_json) != lease.result
                    or tool_call.result_json is None
                ):
                    raise ValueError("Tool replay does not match its saved result")
                if invocation.tool.capability == "workspace.write/v2" or (
                    invocation.tool.capability == "document.convert/v1"
                    and invocation.arguments.get("publish_artifact") is True
                ):
                    from skillmind.artifacts.repository import ArtifactRepository

                    refs = frozenset((lease.result or {}).get("artifact_refs", []))
                    if invocation.tool.capability == "document.convert/v1" and len(refs) != 1:
                        raise ValueError("Conversion replay requires its original Artifact")
                    verified = await ArtifactRepository(session).verified_refs(
                        invocation.run_id, refs,
                    )
                    if verified != refs:
                        raise ValueError("Tool replay Artifact snapshot is unavailable")
            elif tool_call.status != "RUNNING" or lease.status != "RUNNING" or not lease.is_new:
                raise ValueError("Tool invocation does not authorize another dispatch")
            await self._finish(session, gate, locked)

    async def complete(
        self,
        lease: ToolAuditLease,
        *,
        result: dict[str, Any],
        evidence: tuple[EvidenceRecord, ...],
        duration_ms: int,
    ) -> dict[str, Any]:
        """再実行済みなら既存結果を返し、初回だけ Evidence を追加する。"""

        lease, invocation = self._snapshot_lease(lease)
        result = _json_copy(result)
        evidence = _snapshot_evidence(evidence)
        _validate_duration(duration_ms)
        async with self._session_factory() as session, session.begin():
            gate, locked = await self._lock_execution(session)
            tool_call = await self._lock_tool(session, lease, invocation)
            await self._check_document_readiness(session, locked[0], invocation)
            await gate.validate(self._claimed, locked)
            if tool_call.status == "SUCCEEDED" and tool_call.result_json is not None:
                await self._finish(session, gate, locked)
                return _json_copy(tool_call.result_json)
            if tool_call.status != "RUNNING" or not lease.is_new or lease.status != "RUNNING":
                raise ValueError("ToolCall does not authorize completion")
            validate_artifact_publication(invocation, result=result, evidence=evidence)
            artifacts = [record.draft.artifact for record in evidence if record.artifact_ref]
            if artifacts:
                from skillmind.artifacts.repository import ArtifactRepository

                # Run lock が main/child/Attempt を横断する保存総量の直列化点となる。
                saved = await ArtifactRepository(session).list_metadata(
                    project_id=self._claimed.project_id, run_id=tool_call.run_id,
                )
                if len(saved) + len(artifacts) > MAX_RUN_ARTIFACTS or (
                    sum(item.size_bytes for item in saved)
                    + sum(len(item.content) for item in artifacts if item is not None)
                    > MAX_RUN_ARTIFACT_BYTES
                ):
                    raise ValueError("Run Artifact storage limit exceeded")
                await gate.validate(self._claimed, locked)
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
                        artifact_ref=record.artifact_ref,
                        artifact_bytes=(
                            record.draft.artifact.content if record.draft.artifact else None
                        ),
                        artifact_size=(
                            len(record.draft.artifact.content) if record.draft.artifact else None
                        ),
                        artifact_mime_type=(
                            record.draft.artifact.mime_type if record.draft.artifact else None
                        ),
                        artifact_path=(
                            record.draft.artifact.path if record.draft.artifact else None
                        ),
                        created_at=now,
                    )
                    for record in evidence
                ]
            )
            tool_call.status = "SUCCEEDED"
            tool_call.duration_ms = duration_ms
            tool_call.result_json = result
            tool_call.error_json = None
            await self._finish(session, gate, locked)
            return _json_copy(result)

    async def fail(
        self,
        lease: ToolAuditLease,
        *,
        code: str,
        retryable: bool,
        duration_ms: int,
    ) -> None:
        """成功済み呼び出しを上書きせず、安定分類だけを保存する。"""

        lease, invocation = self._snapshot_lease(lease)
        _validate_duration(duration_ms)
        if (
            not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None
            or type(retryable) is not bool
        ):
            raise ValueError("Tool failure classification is invalid")
        async with self._session_factory() as session, session.begin():
            gate, locked = await self._lock_execution(session)
            tool_call = await self._lock_tool(session, lease, invocation)
            await gate.validate(self._claimed, locked)
            if tool_call.status in {"SUCCEEDED", "FAILED", "DENIED"}:
                await self._finish(session, gate, locked)
                return
            if tool_call.status != "RUNNING" or not lease.is_new or lease.status != "RUNNING":
                raise ValueError("ToolCall does not authorize failure completion")
            tool_call.status = "FAILED"
            tool_call.duration_ms = duration_ms
            tool_call.error_json = {"code": code, "retryable": retryable}
            await self._finish(session, gate, locked)

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
        statement = statement.execution_options(populate_existing=True)
        return (await session.scalars(statement)).one_or_none()

    @staticmethod
    async def _check_document_readiness(
        session: AsyncSession, run: Run, invocation: ToolInvocation,
    ) -> None:
        """登録・実行直前・成功保存で同じ正本 gate を使い、Provider の呼び方に依存しない。"""
        from skillmind.runs.document_prerequisites import require_document_readiness

        await require_document_readiness(session, run, invocation.tool.capability)

    async def _lock_execution(
        self, session: AsyncSession,
    ) -> tuple[ToolExecutionGate, tuple[Run, RunSegment | None, RunAttempt]]:
        """ToolCall より先に共有の Run→Segment→Attempt lock を取得する。"""

        # agent の公開 barrel と Run repository の相互 import を初期化時に結ばない。
        from skillmind.runs.tool_execution import ToolExecutionGate

        self._authority_check()
        gate = ToolExecutionGate(
            session, authority_check=self._authority_check, clock=lambda: datetime.now(UTC),
        )
        return gate, await gate._lock_claimed_execution(self._claimed, populate_existing=True)

    async def _lock_tool(
        self, session: AsyncSession, lease: ToolAuditLease, invocation: ToolInvocation,
    ) -> ToolCall:
        """裸 ID で更新せず、固定した原呼出しの全 identity を照合する。"""

        tool_call = await session.get(
            ToolCall, lease.tool_call_id, with_for_update=True, populate_existing=True,
        )
        if tool_call is None:
            raise LookupError("ToolCall is unavailable")
        await self._verify_tool_identity(session, tool_call, invocation)
        return tool_call

    async def _finish(
        self, session: AsyncSession, gate: ToolExecutionGate,
        locked: tuple[Run, RunSegment | None, RunAttempt],
    ) -> None:
        """flush 後にも原 lease を新しい時計で確認し、観測した期限切れを拒否する。"""

        await gate.validate(self._claimed, locked)
        await session.flush()
        await gate.validate(self._claimed, locked)

    def _snapshot_invocation(self, invocation: ToolInvocation) -> ToolInvocation:
        """最初の await より前に nested 引数を切り離し、元の要求摘要も再計算する。"""

        invocation = replace(
            invocation,
            arguments=_json_copy(invocation.arguments),
            tool=replace(invocation.tool, input_schema=_json_copy(invocation.tool.input_schema)),
        )
        if (
            invocation.run_id != self._claimed.run_id
            or invocation.run_attempt_id != self._claimed.run_attempt_id
            or not isinstance(invocation.agent_session_id, UUID)
            or invocation.agent_session_id.int == 0
            or not invocation.sdk_tool_use_id or len(invocation.sdk_tool_use_id) > 128
            or invocation.request_fingerprint != invocation_fingerprint(
                invocation.tool.sdk_name, invocation.arguments,
            )
        ):
            raise ValueError("Tool invocation does not match original Worker authority")
        self._authority_check()
        return invocation

    def _snapshot_lease(self, lease: ToolAuditLease) -> tuple[ToolAuditLease, ToolInvocation]:
        """Test fake の省略可能 field を本番では必須とし、呼出し所有なしの更新を拒否する。"""

        if lease.invocation is None or type(lease.is_new) is not bool:
            raise ValueError("Tool audit lease has no original invocation")
        invocation = self._snapshot_invocation(lease.invocation)
        return replace(
            lease, invocation=invocation, result=_copy_optional(lease.result),
        ), invocation

    async def _verify_tool_identity(
        self, session: AsyncSession, tool_call: ToolCall, invocation: ToolInvocation,
    ) -> None:
        """成功済み RESUME のみ旧 Attempt を読めるが、原行の帰属は決して書き換えない。"""

        if (
            tool_call.run_id != invocation.run_id
            or tool_call.agent_session_id != invocation.agent_session_id
            or tool_call.sdk_tool_use_id != invocation.sdk_tool_use_id
            or tool_call.tool_name != invocation.tool.sdk_name
            or tool_call.capability_version != invocation.tool.capability
            or tool_call.provider != invocation.tool.provider
            or tool_call.integration_id != invocation.tool.integration_id
            or tool_call.request_fingerprint != invocation.request_fingerprint
        ):
            raise ValueError("ToolCall does not match its original invocation")
        if tool_call.run_attempt_id == invocation.run_attempt_id:
            return
        if (
            tool_call.status == "SUCCEEDED"
            and self._claimed.continuation_mode is SessionContinuationMode.RESUME
            and self._claimed.parent_sdk_session_id == invocation.agent_session_id
            and self._claimed.parent_run_attempt_id is not None
        ):
            # Run lock の下で旧 Attempt の所有だけを読み、lock 順の逆転や旧行の再取得を避ける。
            # 複数回 RESUME した同一 SDK session は直前より古い成功を参照できる。
            origin_id = await session.scalar(select(RunAttempt.id).where(
                RunAttempt.id == tool_call.run_attempt_id,
                RunAttempt.run_id == invocation.run_id,
            ))
            if origin_id == tool_call.run_attempt_id:
                return
        raise ValueError("ToolCall belongs to another RunAttempt")


def _snapshot_evidence(evidence: tuple[EvidenceRecord, ...]) -> tuple[EvidenceRecord, ...]:
    """Evidence の公開 ID と nested locator/metadata を、待機前に独立した草稿へ固定する。"""

    records: list[EvidenceRecord] = []
    for record in evidence:
        if (
            not isinstance(record.evidence_ref, str) or len(record.evidence_ref) > 64
            or re.fullmatch(r"ev_[a-zA-Z0-9_-]+", record.evidence_ref) is None
        ):
            raise ValueError("Tool Evidence reference is invalid")
        records.append(EvidenceRecord(
            record.evidence_ref,
            replace(
                record.draft,
                source_locator=_json_copy(record.draft.source_locator),
                metadata=_json_copy(record.draft.metadata or {}),
                artifact=replace(record.draft.artifact) if record.draft.artifact else None,
            ),
            artifact_ref=record.artifact_ref,
        ))
    if not records or len({record.evidence_ref for record in records}) != len(records):
        raise ValueError("Tool Evidence references are missing or duplicated")
    return tuple(records)


def validate_artifact_publication(
    invocation: ToolInvocation, *, result: Mapping[str, Any], evidence: tuple[EvidenceRecord, ...],
) -> None:
    """新 Tool だけが元の UTF-8 出力を発行でき、保存回执と応答を同一内容に固定する。"""

    if (
        invocation.tool.capability == "document.convert/v1"
        and invocation.arguments.get("publish_artifact") is True
    ):
        _validate_conversion_publication(invocation, result=result, evidence=evidence)
        return
    if invocation.tool.capability != "workspace.write/v2":
        if "artifact_refs" in result or any(
            item.artifact_ref is not None or item.draft.artifact is not None for item in evidence
        ):
            raise ValueError("Tool does not authorize Artifact publication")
        return
    path = invocation.arguments.get("path")
    content = invocation.arguments.get("content")
    if (
        invocation.tool.provider != "workspace" or invocation.tool.integration_id is not None
        or not isinstance(path, str) or not isinstance(content, str) or len(evidence) != 1
    ):
        raise ValueError("Artifact publication identity is invalid")
    record = evidence[0]
    draft = record.draft
    data = content.encode("utf-8")
    checksum = f"sha256:{sha256_hex(data)}"
    if (
        result.get("status") != "success" or result.get("provider") != "workspace"
        or result.get("path") != path or result.get("content_hash") != checksum
        or type(result.get("bytes_written")) is not int or result.get("bytes_written") != len(data)
        or result.get("evidence_refs") != [record.evidence_ref]
        or draft.evidence_type != "workspace-write" or draft.content_hash != checksum
        or draft.source_locator != {"path": path, "bytes": len(data)}
        or type(draft.source_locator.get("bytes")) is not int
        or draft.source_uri != f"workspace://runs/{invocation.run_id}/{quote(path, safe='/')}"
        or draft.snapshot_uri is not None
        or draft.metadata != {"scope": "run-workspace", "read_only": False}
        or draft.metadata.get("read_only") is not False
    ):
        raise ValueError("Artifact publication does not match the original write")
    if path.startswith("output/"):
        if (
            not isinstance(record.artifact_ref, str)
            or re.fullmatch(r"art_[a-f0-9]{32}", record.artifact_ref) is None
            or draft.artifact is None or draft.artifact.path != path
            or draft.artifact.content != data or draft.artifact.mime_type != "text/plain"
            or result.get("artifact_refs") != [record.artifact_ref]
        ):
            raise ValueError("Artifact snapshot is missing or inconsistent")
    elif not path.startswith("workspace/") or (
        record.artifact_ref is not None or draft.artifact is not None
        or result.get("artifact_refs") != []
    ):
        raise ValueError("Workspace intermediate files are not Artifact publications")


def _validate_conversion_publication(
    invocation: ToolInvocation, *, result: Mapping[str, Any], evidence: tuple[EvidenceRecord, ...],
) -> None:
    """原本 Evidence と変換 byte を同一 Tool transaction に保存し、参照の欠落を拒否する。"""

    if (
        invocation.tool.provider != "project-documents"
        or invocation.tool.integration_id is not None
        or len(evidence) != 2 or result.get("status") != "success"
        or result.get("provider") != "project"
    ):
        raise ValueError("Conversion Artifact publication identity is invalid")
    artifact, fields = conversion_artifact(result, run_id=invocation.run_id)
    source, converted = evidence
    if (
        source.draft.evidence_type != "document"
        or source.draft.content_hash != fields["source_locator"]["source_checksum"]
        or source.draft.source_locator.get("document_id") != fields["source_locator"]["document_id"]
        or (source.draft.metadata or {}).get("markdown_checksum") != artifact.checksum
        or (source.draft.metadata or {}).get("converter") != fields["metadata"]["converter"]
        or source.artifact_ref is not None or source.draft.artifact is not None
        or converted.draft.artifact != artifact
        or any(getattr(converted.draft, name) != value for name, value in fields.items())
        or not isinstance(converted.artifact_ref, str)
        or re.fullmatch(r"art_[a-f0-9]{32}", converted.artifact_ref) is None
        or result.get("artifact") != conversion_artifact_description(artifact)
        or result.get("artifact_refs") != [converted.artifact_ref]
        or result.get("evidence_refs") != [source.evidence_ref, converted.evidence_ref]
        or source.evidence_ref == converted.evidence_ref
    ):
        raise ValueError("Conversion Artifact does not match its original Markdown")


def _validate_duration(duration_ms: int) -> None:
    """監査の経過時間へ bool/負値を保存しない。"""

    if type(duration_ms) is not int or duration_ms < 0:
        raise ValueError("Tool duration must be a non-negative integer")


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

    return None if value is None else _json_copy(value)


def _reject_sensitive_keys(value: Any) -> None:
    """Evidence metadata へ credential らしい field が混入することを拒否する。"""

    if find_sensitive_key(value) is not None:
        raise ValueError("Evidence metadata contains a sensitive field")
