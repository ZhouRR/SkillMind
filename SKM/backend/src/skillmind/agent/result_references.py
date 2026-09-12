"""Outcome の参照位置と、モデルが主張する effect の保存済み監査を照合する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    Evidence,
    Run,
    ToolCall,
)
from skillmind.effects.catalog import EFFECT_CAPABILITIES
from skillmind.effects.outcomes import effect_requires_reconciliation


@dataclass(frozen=True, slots=True)
class EffectSummaryClaim:
    """Schema 通過後のモデル申告。自由文の summary は監査事実の根拠にしない。"""

    proposal_ref: str
    status: str
    before_ref: str | None
    after_ref: str | None


@dataclass(frozen=True, slots=True)
class ResultReferences:
    """同じ参照が複数の契約位置にあっても一度だけ所有を確認する集合。"""

    evidence: frozenset[str]
    artifacts: frozenset[str]
    proposals: frozenset[str]
    effects: tuple[EffectSummaryClaim, ...]


def collect_result_references(value: Mapping[str, Any], *, outcome: bool) -> ResultReferences:
    """業務 JSON の任意文字列を権限にせず、既存 Evidence convention と包絡位置を収集する。"""

    evidence = _evidence_conventions(value)
    artifacts = _string_refs(value.get("artifact_refs"))
    proposals = _string_refs(value.get("change_proposal_refs"))
    effects: list[EffectSummaryClaim] = []
    if outcome:
        for deliverable in value["deliverables"]:
            if "artifact_ref" in deliverable:
                artifacts.add(deliverable["artifact_ref"])
        for effect in value["effects"]:
            claim = EffectSummaryClaim(
                proposal_ref=effect["proposal_ref"], status=effect["status"],
                before_ref=effect.get("before_ref"), after_ref=effect.get("after_ref"),
            )
            effects.append(claim)
            proposals.add(claim.proposal_ref)
            evidence.update(ref for ref in (claim.before_ref, claim.after_ref) if ref is not None)
    return ResultReferences(
        evidence=frozenset(evidence), artifacts=frozenset(artifacts),
        proposals=frozenset(proposals), effects=tuple(effects),
    )


class EffectSummaryLookup(Protocol):
    """保存された Proposal/Approval/Effect/read-back を一貫した読取で確認する port。"""

    async def invalid_refs(
        self, run_id: UUID, claims: tuple[EffectSummaryClaim, ...],
    ) -> frozenset[str]:
        """同 Run の保存事実と一致しない主張の Proposal ref を返す。"""

        ...


class PostgresEffectSummaryLookup:
    """単一 SELECT の snapshot を使い、別の時点/Run の記録を合成しない。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """外部 I/O や再批准を行わない読取 session factory を保持する。"""

        self._session_factory = session_factory

    async def invalid_refs(
        self, run_id: UUID, claims: tuple[EffectSummaryClaim, ...],
    ) -> frozenset[str]:
        """欠落/壊れた関連は拒否し、今日の Integration 状態で歴史を書き換えない。"""

        refs = frozenset(claim.proposal_ref for claim in claims)
        if not refs:
            return frozenset()
        before = aliased(Evidence, name="effect_before")
        after = aliased(Evidence, name="effect_after")
        statement = (
            select(ChangeProposal, ChangeApproval, EffectExecution, ToolCall, before, after,
                   Run.project_id)
            .select_from(ChangeProposal)
            .join(Run, Run.id == ChangeProposal.run_id)
            .outerjoin(ChangeApproval, ChangeApproval.proposal_id == ChangeProposal.id)
            .outerjoin(EffectExecution, EffectExecution.proposal_id == ChangeProposal.id)
            .outerjoin(ToolCall, ToolCall.id == EffectExecution.tool_call_id)
            .outerjoin(before, before.evidence_ref == EffectExecution.before_ref)
            .outerjoin(after, after.evidence_ref == EffectExecution.after_ref)
            .where(ChangeProposal.run_id == run_id, ChangeProposal.proposal_ref.in_(refs))
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            # UNIQUE 制約の破損も「最初の一行」で成功にしない。
            by_ref: dict[str, list[Any]] = {}
            for row in rows:
                by_ref.setdefault(row[0].proposal_ref, []).append(row)
            return frozenset(
                claim.proposal_ref for claim in claims
                if len(by_ref.get(claim.proposal_ref, ())) != 1
                or not _matches_claim(run_id, claim, *by_ref[claim.proposal_ref][0])
            )


def _matches_claim(
    run_id: UUID, claim: EffectSummaryClaim, proposal: ChangeProposal,
    approval: ChangeApproval | None, execution: EffectExecution | None,
    tool: ToolCall | None, before: Evidence | None, after: Evidence | None,
    project_id: UUID,
) -> bool:
    """状態文字列だけでなく原批准と実行の結線を検査し、遠端の現在値は主張しない。"""

    if proposal.run_id != run_id or proposal.project_id != project_id:
        return False
    expected_status = "PROPOSED" if proposal.status == "DRAFT" else proposal.status
    if claim.status != expected_status or proposal.status in {
        "PENDING_APPROVAL", "APPROVED", "APPLYING",
    }:
        return False
    if approval is None:
        # 未批准の草稿と批准期限切れには実行記録が無い。STALE は未書込の証明ではない。
        return (
            proposal.status in {"DRAFT", "STALE"} and execution is None
            and claim.before_ref is None and claim.after_ref is None
        )
    capability = EFFECT_CAPABILITIES.get(proposal.capability_version)
    if (
        capability is None or approval.proposal_id != proposal.id or approval.run_id != run_id
        or type(proposal.version) is not int or proposal.version < 1
        or approval.proposal_version != proposal.version or not _checksum(proposal.checksum)
        or approval.proposal_checksum != proposal.checksum
    ):
        return False
    if approval.source == "USER":
        if not _identity(approval.actor_id) or approval.preauthorization_id is not None:
            return False
    elif approval.source == "PREAUTHORIZATION":
        if (not capability.preauthorizable or proposal.risk_level != "LOW"
                or approval.actor_id is not None or not _identity(approval.preauthorization_id)):
            return False
    else:
        return False
    if proposal.status == "REJECTED":
        return (
            approval.decision == "REJECTED" and execution is None
            and claim.before_ref is None and claim.after_ref is None
        )
    if (
        approval.decision != "APPROVED" or execution is None
        or execution.run_id != run_id or execution.proposal_id != proposal.id
        or execution.approval_id != approval.id
        or execution.idempotency_key != proposal.idempotency_key
        or execution.request_fingerprint != proposal.request_fingerprint
        or capability.provider_versions.get(execution.provider) != execution.provider_version
    ):
        return False
    expected_execution = {
        "APPLIED": {"APPLIED"}, "STALE": {"STALE"},
        "FAILED": {"FAILED", "VERIFICATION_FAILED"},
    }.get(proposal.status, set())
    if (execution.status not in expected_execution
        or effect_requires_reconciliation(execution.error_json)):
        return False
    if (claim.before_ref is not None and claim.before_ref != execution.before_ref) or (
        claim.after_ref is not None and claim.after_ref != execution.after_ref
    ):
        return False
    if execution.tool_call_id is not None and (
        tool is None or tool.id != execution.tool_call_id or tool.run_id != run_id
        or tool.capability_version != proposal.capability_version
        or tool.provider != execution.provider or tool.integration_id != proposal.integration_id
        or tool.request_fingerprint != execution.request_fingerprint
    ):
        return False
    if proposal.status != "APPLIED":
        # 未 claim の拒否には ToolCall が無い。失敗は外部無変更/rollback と解釈しない。
        return (
            execution.before_ref is None and execution.after_ref is None
            and (tool is None or (tool.status == "FAILED" and tool.result_json is None))
        )
    if (
        tool is None or tool.id != execution.tool_call_id or tool.run_id != run_id
        or tool.status != "SUCCEEDED"
        or execution.error_json is not None or execution.executed_at is None
        or before is None or after is None or before.evidence_ref == after.evidence_ref
    ):
        return False
    if any(
        evidence.run_id != run_id or evidence.tool_call_id != tool.id
        or evidence.evidence_ref != ref or not _saved_evidence_matches(evidence)
        for evidence, ref in ((before, execution.before_ref), (after, execution.after_ref))
    ):
        return False
    expected = proposal.verification_json
    observed = execution.verification_json
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return False
    expected_paths = _unique_paths(expected.get("paths"))
    observed_paths = _unique_paths(observed.get("matched_paths"))
    tool_result = tool.result_json
    return (
        expected.get("method") == observed.get("method") == "READ_BACK"
        and expected_paths is not None and expected_paths == observed_paths
        and isinstance(tool_result, dict) and tool.error_json is None
        and tool_result.get("status") == "success"
        and tool_result.get("provider") == execution.provider
        and tool_result.get("proposal_ref") == proposal.proposal_ref
        and tool_result.get("before_ref") == execution.before_ref
        and tool_result.get("after_ref") == execution.after_ref
        and tool_result.get("verification") == observed
    )


def _saved_evidence_matches(evidence: Evidence) -> bool:
    """Effect の既存 snapshot と hash を再計算し、文字列の prefix だけを検証にしない。"""

    metadata = evidence.metadata_json
    snapshot = metadata.get("snapshot") if isinstance(metadata, dict) else None
    if not isinstance(snapshot, dict) or not _checksum(evidence.content_hash):
        return False
    try:
        return evidence.content_hash == f"sha256:{sha256_hex(canonical_json(snapshot))}"
    except (TypeError, ValueError):
        return False


def _unique_paths(value: Any) -> frozenset[str] | None:
    """空/重複/非文字列の read-back 対象を有効な完全一致へ丸めない。"""

    if not isinstance(value, list) or not value or not all(
        isinstance(path, str) and path.startswith("/") for path in value
    ):
        return None
    paths = frozenset(value)
    return paths if len(paths) == len(value) else None


def _checksum(value: Any) -> bool:
    """保存された content identity の形式を確認し、本文の再取得とは区別する。"""

    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _identity(value: Any) -> bool:
    """NULL/nil を有効な批准主体へ昇格させない。"""

    return isinstance(value, UUID) and value.int != 0


def _evidence_conventions(value: Any) -> set[str]:
    """旧業務 Schema の source_ref/evidence_refs convention を維持する。"""

    refs: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key == "source_ref" and isinstance(nested, str):
                refs.add(nested)
            elif key == "evidence_refs" and isinstance(nested, list):
                refs.update(item for item in nested if isinstance(item, str))
            else:
                refs.update(_evidence_conventions(nested))
    elif isinstance(value, list | tuple):
        for nested in value:
            refs.update(_evidence_conventions(nested))
    return refs


def _string_refs(value: Any) -> set[str]:
    """既存の top-level convention を、意味のない非文字列の暗黙変換なしで収集する。"""

    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()
