"""Result の effect 主張を、実 ORM の結線と一回の SQL 読取で照合する。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import and_

from skillmind.agent.result_references import (
    EffectSummaryClaim,
    PostgresEffectSummaryLookup,
)
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    Evidence,
    ToolCall,
)
from skillmind.effects.catalog import EFFECT_CAPABILITIES
from skillmind.effects.domain import ClaimedEffectExecution, EffectEvidenceDraft
from skillmind.effects.redmine import RedmineIssueSnapshot, RedmineIssueUpdateProvider
from skillmind.effects.repository_effect import _result as repository_effect_result
from skillmind.runs.repository_effects import EffectOperationsMixin


@dataclass(frozen=True, slots=True)
class EffectGraph:
    """本番と同じ一意関連を持つ、DB 接続不要の保存済み effect graph。"""

    proposal: ChangeProposal
    approval: ChangeApproval
    execution: EffectExecution
    tool: ToolCall
    before: Evidence
    after: Evidence
    project_id: UUID

    def joined(self, *, omit: frozenset[str] = frozenset()) -> tuple[object, ...]:
        """LEFT OUTER JOIN の欠落も含め、production SELECT の列順を再現する。"""

        return tuple(
            None if name in omit else getattr(self, name)
            for name in (
                "proposal", "approval", "execution", "tool", "before", "after", "project_id",
            )
        )

    def claim(self, *, include_evidence: bool = True) -> EffectSummaryClaim:
        """任意 Evidence ref を省略しても同じ保存済み実行を主張する。"""

        return EffectSummaryClaim(
            proposal_ref=self.proposal.proposal_ref,
            status="PROPOSED" if self.proposal.status == "DRAFT" else self.proposal.status,
            before_ref=self.execution.before_ref if include_evidence else None,
            after_ref=self.execution.after_ref if include_evidence else None,
        )


def _graph(*, provider: str = "redmine") -> EffectGraph:
    """現在の三 Provider に共通する APPLIED 記録を、既存 Evidence writer で作る。"""

    now = datetime(2026, 8, 1, tzinfo=UTC)
    project_id, run_id, integration_id = uuid4(), uuid4(), uuid4()
    capability_name = "issue.update/v1" if provider == "redmine" else "repository.write/v1"
    capability = EFFECT_CAPABILITIES[capability_name]
    paths = ["/fields/status_id"] if provider == "redmine" else ["/files/src/handler.py"]
    proposal = ChangeProposal(
        id=uuid4(), proposal_ref=f"cp_{uuid4().hex}", project_id=project_id, run_id=run_id,
        run_attempt_id=uuid4(), capability_version=capability_name, integration_id=integration_id,
        status="APPLIED", version=1, checksum=f"sha256:{'1' * 64}", risk_level="LOW",
        idempotency_key="original-effect", request_fingerprint="2" * 64,
        verification_json={"method": "READ_BACK", "paths": paths},
    )
    approval = ChangeApproval(
        id=uuid4(), proposal_id=proposal.id, run_id=run_id, source="USER", decision="APPROVED",
        actor_id=uuid4(), preauthorization_id=None, proposal_version=proposal.version,
        proposal_checksum=proposal.checksum,
    )
    execution = EffectExecution(
        id=uuid4(), proposal_id=proposal.id, run_id=run_id, approval_id=approval.id,
        tool_call_id=uuid4(), status="APPLIED", provider=provider,
        provider_version=capability.provider_versions[provider],
        idempotency_key=proposal.idempotency_key, request_fingerprint=proposal.request_fingerprint,
        before_ref=f"ev_{uuid4().hex}", after_ref=f"ev_{uuid4().hex}",
        verification_json={"method": "READ_BACK", "matched_paths": paths, "replayed": False},
        error_json=None, executed_at=now,
    )
    tool = ToolCall(
        id=execution.tool_call_id, run_id=run_id, run_attempt_id=proposal.run_attempt_id,
        sdk_tool_use_id=f"effect:{execution.id}", status="SUCCEEDED",
        capability_version=capability_name, provider=provider, integration_id=integration_id,
        request_fingerprint=execution.request_fingerprint, error_json=None,
        result_json={
            "status": "success", "provider": provider, "proposal_ref": proposal.proposal_ref,
            "before_ref": execution.before_ref, "after_ref": execution.after_ref,
            "verification": dict(execution.verification_json), "replayed": False,
        },
    )

    def evidence(ref: str | None, phase: str) -> Evidence:
        """原 writer の canonical snapshot/hash 形式を test 側で複製しない。"""

        assert ref is not None
        return EffectOperationsMixin._effect_evidence_row(
            run_id=run_id, tool_call_id=tool.id, evidence_ref=ref, now=now,
            draft=EffectEvidenceDraft(
                evidence_type=f"issue.update.{phase}" if provider == "redmine" else "source_code",
                source_uri=f"{provider}://integration/{integration_id}/revision",
                source_locator={"phase": phase}, content={"phase": phase, "value": "保存済み"},
                excerpt=None, metadata={"capability": capability_name},
            ),
        )

    return EffectGraph(
        proposal, approval, execution, tool,
        evidence(execution.before_ref, "before"), evidence(execution.after_ref, "after"),
        project_id,
    )


def _lookup(*rows: tuple[object, ...]) -> tuple[PostgresEffectSummaryLookup, MagicMock, AsyncMock]:
    """SQL 文自体は本番から取得し、外部 DB の応答だけを差し替える。"""

    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = list(rows)
    session.execute.return_value = result
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    return PostgresEffectSummaryLookup(factory), factory, session


async def _invalid(graph: EffectGraph) -> frozenset[str]:
    """変更した実 ORM graph を production lookup に渡す。"""

    lookup, _, _ = _lookup(graph.joined())
    return await lookup.invalid_refs(graph.proposal.run_id, (graph.claim(),))


async def test_empty_claims_do_not_open_a_database_session() -> None:
    """外部効果の申告が無い結果は追加の DB 読取を要求しない。"""

    lookup, factory, session = _lookup()
    assert await lookup.invalid_refs(uuid4(), ()) == frozenset()
    factory.assert_not_called()
    session.execute.assert_not_awaited()


async def test_lookup_uses_one_scoped_snapshot_and_preserves_missing_outer_joins() -> None:
    """Run/ref と六つの関連を一 SELECT で読み、欠落を INNER JOIN で隠さない。"""

    graph = _graph()
    lookup, _, session = _lookup(graph.joined())
    assert await lookup.invalid_refs(graph.proposal.run_id, (graph.claim(),)) == frozenset()
    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    assert statement.whereclause.compare(and_(
        ChangeProposal.run_id == graph.proposal.run_id,
        ChangeProposal.proposal_ref.in_(frozenset({graph.proposal.proposal_ref})),
    ))
    sql = " ".join(str(statement).split())
    for clause in (
        "JOIN runs ON runs.id = change_proposals.run_id",
        "LEFT OUTER JOIN change_approvals ON change_approvals.proposal_id = change_proposals.id",
        "LEFT OUTER JOIN effect_executions ON effect_executions.proposal_id = change_proposals.id",
        "LEFT OUTER JOIN tool_calls ON tool_calls.id = effect_executions.tool_call_id",
        "LEFT OUTER JOIN evidence AS effect_before "
        "ON effect_before.evidence_ref = effect_executions.before_ref",
        "LEFT OUTER JOIN evidence AS effect_after "
        "ON effect_after.evidence_ref = effect_executions.after_ref",
    ):
        assert clause in sql
    assert len(statement.column_descriptions) == 7
    assert statement.column_descriptions[-1]["name"] == "project_id"
    assert "FOR UPDATE" not in sql
    assert "JOIN integrations" not in sql
    assert "JOIN resource_bindings" not in sql
    session.commit.assert_not_awaited()
    session.flush.assert_not_awaited()


@pytest.mark.parametrize("provider", ["redmine", "git", "svn"])
@pytest.mark.parametrize("include_evidence", [True, False])
async def test_current_provider_records_match_with_or_without_optional_claim_refs(
    provider: str, include_evidence: bool,
) -> None:
    """既存 Provider 三種の保存形式を受理し、模型に任意 ref の再掲を強制しない。"""

    graph = _graph(provider=provider)
    lookup, _, _ = _lookup(graph.joined())
    assert await lookup.invalid_refs(
        graph.proposal.run_id, (graph.claim(include_evidence=include_evidence),),
    ) == frozenset()


@pytest.mark.parametrize("missing", ["approval", "execution", "tool", "before", "after"])
async def test_applied_requires_every_persisted_link(missing: str) -> None:
    """APPLIED 文字列だけでは批准・実行・原 Tool・前後 snapshot 欠落を補わない。"""

    graph = _graph()
    lookup, _, _ = _lookup(graph.joined(omit=frozenset({missing})))
    assert await lookup.invalid_refs(graph.proposal.run_id, (graph.claim(),)) == frozenset({
        graph.proposal.proposal_ref,
    })


@pytest.mark.parametrize(("target", "field", "value"), [
    ("proposal", "project_id", uuid4()),
    ("proposal", "version", 0),
    ("proposal", "version", True),
    ("proposal", "checksum", "sha256:invalid"),
    ("proposal", "capability_version", "unregistered.write/v1"),
    ("approval", "run_id", uuid4()),
    ("approval", "proposal_id", uuid4()),
    ("approval", "proposal_version", 2),
    ("approval", "proposal_checksum", "sha256:" + "a" * 64),
    ("approval", "decision", "REJECTED"),
    ("approval", "source", "UNKNOWN"),
    ("approval", "actor_id", None),
    ("approval", "actor_id", UUID(int=0)),
    ("approval", "preauthorization_id", uuid4()),
    ("execution", "run_id", uuid4()),
    ("execution", "proposal_id", uuid4()),
    ("execution", "approval_id", uuid4()),
    ("execution", "tool_call_id", uuid4()),
    ("execution", "idempotency_key", "different-original-effect"),
    ("execution", "request_fingerprint", "a" * 64),
    ("execution", "provider", "unknown"),
    ("execution", "provider_version", "redmine-cas/unknown"),
    ("execution", "status", "VERIFICATION_FAILED"),
    ("execution", "executed_at", None),
    ("execution", "error_json", {"code": "transport_error"}),
    ("tool", "run_id", uuid4()),
    ("tool", "status", "FAILED"),
    ("tool", "capability_version", "repository.write/v1"),
    ("tool", "provider", "git"),
    ("tool", "integration_id", uuid4()),
    ("tool", "request_fingerprint", "a" * 64),
    ("before", "run_id", uuid4()),
    ("after", "run_id", uuid4()),
    ("before", "tool_call_id", uuid4()),
    ("after", "tool_call_id", uuid4()),
    ("before", "evidence_ref", "ev_another"),
    ("after", "evidence_ref", "ev_another"),
    ("before", "content_hash", "sha256:invalid"),
    ("after", "content_hash", "sha256:" + "a" * 64),
    ("before", "metadata_json", {}),
    ("after", "metadata_json", {"snapshot": None}),
    ("after", "metadata_json", {"snapshot": {"value": "changed"}}),
])
async def test_mismatched_saved_identity_or_content_is_rejected(
    target: str, field: str, value: object,
) -> None:
    """別 Run/批准版/要求/Tool/byte identity の関連を正しい ref 外形で通さない。"""

    graph = _graph()
    setattr(getattr(graph, target), field, value)
    assert await _invalid(graph) == frozenset({graph.proposal.proposal_ref})


async def test_another_run_proposal_is_rejected_even_when_all_internal_links_match() -> None:
    """同 Project の別 Run を、SQL filter とは独立した graph check でも拒否する。"""

    graph = _graph()
    lookup, _, _ = _lookup(graph.joined())
    assert await lookup.invalid_refs(uuid4(), (graph.claim(),)) == frozenset({
        graph.proposal.proposal_ref,
    })


async def test_missing_duplicate_and_conflicting_claims_cannot_be_collapsed_to_success() -> None:
    """存在しない ref、壊れた唯一性、一件でも矛盾する重複主張を拒否する。"""

    graph = _graph()
    claim = graph.claim()
    lookup, _, _ = _lookup()
    assert await lookup.invalid_refs(graph.proposal.run_id, (claim,)) == frozenset({
        claim.proposal_ref,
    })
    duplicate, _, _ = _lookup(graph.joined(), graph.joined())
    assert await duplicate.invalid_refs(graph.proposal.run_id, (claim,)) == frozenset({
        claim.proposal_ref,
    })
    lookup, _, _ = _lookup(graph.joined())
    assert await lookup.invalid_refs(
        graph.proposal.run_id, (claim, replace(claim, status="FAILED")),
    ) == frozenset({claim.proposal_ref})
    assert await lookup.invalid_refs(graph.proposal.run_id, (claim, claim)) == frozenset()


@pytest.mark.parametrize("field", ["before_ref", "after_ref"])
async def test_claim_cannot_substitute_another_same_run_evidence(field: str) -> None:
    """同 Run の存在確認だけでなく当該 Effect が保存した ref の一致を要求する。"""

    graph = _graph()
    lookup, _, _ = _lookup(graph.joined())
    claim = replace(graph.claim(), **{field: "ev_different_same_run"})
    assert await lookup.invalid_refs(graph.proposal.run_id, (claim,)) == frozenset({
        claim.proposal_ref,
    })


async def test_same_evidence_cannot_stand_for_both_observation_phases() -> None:
    """before と after の内容が同じ replay でも Evidence identity は二つ必要。"""

    graph = _graph()
    graph.execution.after_ref = graph.execution.before_ref
    lookup, _, _ = _lookup(replace(graph, after=graph.before).joined())
    assert await lookup.invalid_refs(graph.proposal.run_id, (graph.claim(),)) == frozenset({
        graph.proposal.proposal_ref,
    })


@pytest.mark.parametrize(("target", "field", "value"), [
    ("proposal", "method", "CHECK_ONLY"),
    ("execution", "method", "WRITE_RESPONSE"),
    ("proposal", "paths", []),
    ("proposal", "paths", ["/fields/status_id", "/fields/status_id"]),
    ("execution", "matched_paths", []),
    ("execution", "matched_paths", ["/fields/another"]),
    ("execution", "matched_paths", ["/fields/status_id", "/fields/unapproved"]),
    ("execution", "matched_paths", ["/fields/status_id", "/fields/status_id"]),
    ("execution", "matched_paths", ["fields/status_id"]),
    ("execution", "matched_paths", [1]),
    ("execution", "matched_paths", "/fields/status_id"),
])
async def test_read_back_must_cover_exactly_the_approved_distinct_paths(
    target: str, field: str, value: Any,
) -> None:
    """READ_BACK 名だけや部分/重複対象を、批准済み全対象の確認と扱わない。"""

    graph = _graph()
    getattr(graph, target).verification_json[field] = value
    assert await _invalid(graph) == frozenset({graph.proposal.proposal_ref})


async def test_replayed_read_back_without_revision_fields_remains_compatible() -> None:
    """既存 Redmine replay の保存形式を、新しい revision 必須条件で拒否しない。"""

    graph = _graph()
    graph.execution.verification_json["replayed"] = True
    assert graph.tool.result_json is not None
    graph.tool.result_json["replayed"] = True
    graph.tool.result_json["verification"] = dict(graph.execution.verification_json)
    assert await _invalid(graph) == frozenset()


async def test_snapshot_hash_uses_canonical_content_not_mapping_insertion_order() -> None:
    """保存済み JSON の key 順序変更は内容破損ではなく、実値変更は破損と扱う。"""

    graph = _graph()
    snapshot = {"z": "末尾", "a": 1}
    graph.after.metadata_json = {"snapshot": snapshot}
    graph.after.content_hash = f"sha256:{sha256_hex(canonical_json(snapshot))}"
    graph.after.metadata_json = {"snapshot": {"a": 1, "z": "末尾"}}
    assert await _invalid(graph) == frozenset()
    graph.after.metadata_json["snapshot"]["a"] = 2
    assert await _invalid(graph) == frozenset({graph.proposal.proposal_ref})


@pytest.mark.parametrize("snapshot", [{"invalid": float("nan")}, {"invalid": object()}])
async def test_non_json_snapshot_fails_closed_without_escaping_as_a_serializer_error(
    snapshot: dict[str, object],
) -> None:
    """壊れた snapshot は分類済み不一致とし、本文や serializer error を公開しない。"""

    graph = _graph()
    graph.after.metadata_json = {"snapshot": snapshot}
    assert await _invalid(graph) == frozenset({graph.proposal.proposal_ref})


@pytest.mark.parametrize("status", ["DRAFT", "STALE"])
async def test_unapproved_draft_or_expired_proposal_needs_no_execution(status: str) -> None:
    """批准前の草稿/期限切れを許し、Tool 成功や遠端無変更を補造しない。"""

    graph = _graph()
    graph.proposal.status = status
    lookup, _, _ = _lookup(graph.joined(omit=frozenset({
        "approval", "execution", "tool", "before", "after",
    })))
    assert await lookup.invalid_refs(
        graph.proposal.run_id, (graph.claim(include_evidence=False),),
    ) == frozenset()
    assert await lookup.invalid_refs(graph.proposal.run_id, (graph.claim(),)) == frozenset({
        graph.proposal.proposal_ref,
    })


async def test_rejection_requires_exact_negative_approval_and_no_execution() -> None:
    """REJECTED は原版の拒否判断を持ち、同時に実行が有れば整合済みにしない。"""

    graph = _graph()
    graph.proposal.status = "REJECTED"
    graph.approval.decision = "REJECTED"
    claim = graph.claim(include_evidence=False)
    lookup, _, _ = _lookup(graph.joined(omit=frozenset({
        "execution", "tool", "before", "after",
    })))
    assert await lookup.invalid_refs(graph.proposal.run_id, (claim,)) == frozenset()
    graph.approval.proposal_checksum = "sha256:" + "a" * 64
    assert await lookup.invalid_refs(graph.proposal.run_id, (claim,)) == frozenset({
        claim.proposal_ref,
    })
    graph.approval.proposal_checksum = graph.proposal.checksum
    lookup, _, _ = _lookup(graph.joined())
    assert await lookup.invalid_refs(graph.proposal.run_id, (claim,)) == frozenset({
        claim.proposal_ref,
    })


@pytest.mark.parametrize(("proposal_status", "execution_status"), [
    ("STALE", "STALE"), ("FAILED", "FAILED"), ("FAILED", "VERIFICATION_FAILED"),
])
async def test_terminal_failure_without_provider_tool_preserves_unknown_remote_outcome(
    proposal_status: str, execution_status: str,
) -> None:
    """Provider 前の終局も表現し、失敗を成功 Evidence や rollback に置き換えない。"""

    graph = _graph()
    graph.proposal.status = proposal_status
    graph.execution.status = execution_status
    graph.execution.tool_call_id = None
    graph.execution.before_ref = None
    graph.execution.after_ref = None
    graph.execution.error_json = {"code": "unavailable", "retryable": False}
    lookup, _, _ = _lookup(graph.joined(omit=frozenset({"tool", "before", "after"})))
    assert await lookup.invalid_refs(graph.proposal.run_id, (graph.claim(),)) == frozenset()


def _failure_graph(execution_status: str) -> EffectGraph:
    """Provider に到達した失敗を、既存 finalize と同じ Tool/Effect 状態で組む。"""

    graph = _graph()
    graph.proposal.status = "STALE" if execution_status == "STALE" else "FAILED"
    graph.execution.status = execution_status
    graph.execution.before_ref = None
    graph.execution.after_ref = None
    graph.execution.verification_json = {}
    graph.execution.error_json = {"code": "unavailable", "retryable": False}
    graph.tool.status = "FAILED"
    graph.tool.result_json = None
    graph.tool.error_json = {"code": "unavailable", "retryable": False}
    return graph


@pytest.mark.parametrize("status", ["STALE", "FAILED", "VERIFICATION_FAILED"])
async def test_terminal_failure_with_matching_failed_tool_remains_valid(status: str) -> None:
    """実行済み Tool が FAILED と保存された正規の失敗結果は拒否しない。"""

    graph = _failure_graph(status)
    lookup, _, _ = _lookup(graph.joined(omit=frozenset({"before", "after"})))
    assert await lookup.invalid_refs(graph.proposal.run_id, (graph.claim(),)) == frozenset()


@pytest.mark.parametrize("status", ["STALE", "FAILED", "VERIFICATION_FAILED"])
@pytest.mark.parametrize(("field", "value"), [
    ("id", uuid4()),
    ("run_id", uuid4()),
    ("capability_version", "repository.write/v1"),
    ("provider", "git"),
    ("integration_id", uuid4()),
    ("request_fingerprint", "a" * 64),
    ("status", "SUCCEEDED"),
    ("status", "RUNNING"),
    ("result_json", {"status": "success"}),
])
async def test_failure_cannot_ignore_a_present_but_inconsistent_tool(
    status: str, field: str, value: object,
) -> None:
    """失敗でも別 Run/原要求/成功 response を混ぜず、旧 recovery の RUNNING を補正しない。"""

    graph = _failure_graph(status)
    setattr(graph.tool, field, value)
    lookup, _, _ = _lookup(graph.joined(omit=frozenset({"before", "after"})))
    assert await lookup.invalid_refs(graph.proposal.run_id, (graph.claim(),)) == frozenset({
        graph.proposal.proposal_ref,
    })


@pytest.mark.parametrize("status", ["STALE", "FAILED", "VERIFICATION_FAILED"])
async def test_failure_with_missing_linked_tool_is_not_a_pre_provider_failure(status: str) -> None:
    """Tool ID が保存済みなのに行が無い事実を、未 claim の合法な NULL と区別する。"""

    graph = _failure_graph(status)
    assert graph.execution.tool_call_id is not None
    lookup, _, _ = _lookup(graph.joined(omit=frozenset({"tool", "before", "after"})))
    assert await lookup.invalid_refs(graph.proposal.run_id, (graph.claim(),)) == frozenset({
        graph.proposal.proposal_ref,
    })


@pytest.mark.parametrize("stored_status", [
    "DRAFT", "PENDING_APPROVAL", "APPROVED", "APPLYING", "APPLIED", "REJECTED", "STALE", "FAILED",
])
@pytest.mark.parametrize("claim_status", [
    "PROPOSED", "APPROVED", "REJECTED", "APPLIED", "FAILED", "STALE",
])
async def test_every_outcome_status_requires_its_exact_terminal_platform_state(
    stored_status: str, claim_status: str,
) -> None:
    """包絡の全 status と Proposal の全 status を交差し、近い意味への読み替えを許さない。"""

    graph = _failure_graph("FAILED") if stored_status == "FAILED" else _graph()
    graph.proposal.status = stored_status
    omitted: set[str] = set()
    if stored_status in {"DRAFT", "PENDING_APPROVAL", "STALE"}:
        omitted.update({"approval", "execution", "tool", "before", "after"})
    elif stored_status == "REJECTED":
        graph.approval.decision = "REJECTED"
        omitted.update({"execution", "tool", "before", "after"})
    elif stored_status in {"APPROVED", "APPLYING"}:
        graph.execution.status = "REQUESTED" if stored_status == "APPROVED" else "APPLYING"
        graph.execution.before_ref = None
        graph.execution.after_ref = None
        omitted.update({"before", "after"})
        if stored_status == "APPROVED":
            graph.execution.tool_call_id = None
            omitted.add("tool")
        else:
            graph.tool.status = "RUNNING"
            graph.tool.result_json = None
    elif stored_status == "FAILED":
        omitted.update({"before", "after"})
    lookup, _, _ = _lookup(graph.joined(omit=frozenset(omitted)))
    claim = replace(graph.claim(include_evidence=False), status=claim_status)
    accepted = {
        ("DRAFT", "PROPOSED"), ("APPLIED", "APPLIED"), ("REJECTED", "REJECTED"),
        ("STALE", "STALE"), ("FAILED", "FAILED"),
    }
    expected = (
        frozenset()
        if (stored_status, claim_status) in accepted
        else frozenset({claim.proposal_ref})
    )
    assert await lookup.invalid_refs(graph.proposal.run_id, (claim,)) == expected


@pytest.mark.parametrize("status", ["PENDING_APPROVAL", "APPROVED", "APPLYING"])
async def test_incomplete_proposals_do_not_become_valid_terminal_summaries(status: str) -> None:
    """承認/実行途中を模型の同名 status だけで完了扱いにしない。"""

    graph = _graph()
    graph.proposal.status = status
    assert await _invalid(graph) == frozenset({graph.proposal.proposal_ref})


@pytest.mark.parametrize("provider", ["redmine", "git", "svn"])
async def test_preauthorization_is_limited_to_existing_low_risk_registered_capability(
    provider: str,
) -> None:
    """保存上も repository 免審を認めず、Redmine LOW の既存判断だけを読む。"""

    graph = _graph(provider=provider)
    graph.approval.source = "PREAUTHORIZATION"
    graph.approval.actor_id = None
    graph.approval.preauthorization_id = uuid4()
    expected = frozenset() if provider == "redmine" else frozenset({graph.proposal.proposal_ref})
    assert await _invalid(graph) == expected
    graph.proposal.risk_level = "HIGH"
    assert await _invalid(graph) == frozenset({graph.proposal.proposal_ref})


async def test_database_read_failure_is_not_converted_to_verified_history() -> None:
    """読取失敗は呼出し側へ伝え、空の不一致集合へ降格しない。"""

    graph = _graph()
    lookup, _, session = _lookup(graph.joined())
    session.execute.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await lookup.invalid_refs(graph.proposal.run_id, (graph.claim(),))


@pytest.mark.parametrize(("field", "value"), [
    ("status", "error"),
    ("provider", "git"),
    ("proposal_ref", "cp_other"),
    ("before_ref", "ev_other_before"),
    ("after_ref", "ev_other_after"),
    ("verification", {"method": "READ_BACK", "matched_paths": ["/fields/unapproved"]}),
    ("verification", None),
])
async def test_tool_saved_response_must_agree_with_its_effect_execution(
    field: str, value: object,
) -> None:
    """同 Tool と Effect に矛盾する保存摘要が有る場合、片方だけを証明に使わない。"""

    graph = _graph()
    assert graph.tool.result_json is not None
    graph.tool.result_json[field] = value
    assert await _invalid(graph) == frozenset({graph.proposal.proposal_ref})


@pytest.mark.parametrize("missing_result", [True, False])
async def test_success_tool_requires_a_saved_response_without_error(missing_result: bool) -> None:
    """SUCCEEDED 文字列と欠落 response/併存 error の矛盾を見逃さない。"""

    graph = _graph()
    if missing_result:
        graph.tool.result_json = None
    else:
        graph.tool.error_json = {"code": "unavailable"}
    assert await _invalid(graph) == frozenset({graph.proposal.proposal_ref})


def _claimed(graph: EffectGraph) -> ClaimedEffectExecution:
    """Provider の純粋な結果生成に使い、接続 client を作らない既存 DTO。"""

    return ClaimedEffectExecution(
        effect_execution_id=graph.execution.id, proposal_id=graph.proposal.id,
        proposal_ref=graph.proposal.proposal_ref, approval_id=graph.approval.id,
        run_id=graph.proposal.run_id, run_segment_id=uuid4(),
        run_attempt_id=graph.proposal.run_attempt_id, agent_session_id=uuid4(),
        project_id=graph.project_id, integration_id=graph.proposal.integration_id,
        binding_id=uuid4(), capability_version=graph.proposal.capability_version,
        operation="update", target={"locator": "42", "display": "Test resource"},
        changes=({"path": "/fields/status_id", "action": "SET", "value": 3},),
        precondition={"revision": "rev-1"}, verification=dict(graph.proposal.verification_json),
        idempotency_key=graph.execution.idempotency_key,
        request_fingerprint=graph.execution.request_fingerprint,
        provider=graph.execution.provider, integration_revision=1, integration_scope={},
        integration_config={"base_url": "https://example.invalid"}, secret_reference_id=None,
        lease_token="test-only", lease_expires_at=datetime(2026, 8, 1, tzinfo=UTC), attempt_no=1,
    )


@pytest.mark.parametrize("provider", ["redmine", "git", "svn"])
@pytest.mark.parametrize("replayed", [False, True])
async def test_actual_provider_result_and_evidence_writer_match_new_result_gate(
    provider: str, replayed: bool,
) -> None:
    """実 Provider の返却形と既存 snapshot writer をつなぎ、架空の保存形式で通さない。"""

    graph = _graph(provider=provider)
    claimed = _claimed(graph)
    if provider == "redmine":
        transport = AsyncMock()
        transport.read_issue.side_effect = (
            [RedmineIssueSnapshot("42", "rev-2", {"status_id": 3})] if replayed else [
                RedmineIssueSnapshot("42", "rev-1", {"status_id": 1}),
                RedmineIssueSnapshot("42", "rev-2", {"status_id": 3}),
            ]
        )
        transport.update_issue.return_value = False
        result = await RedmineIssueUpdateProvider(transport).apply(
            claimed, credential="synthetic-test-only",
        )
        assert transport.read_issue.await_count == (1 if replayed else 2)
        assert transport.update_issue.await_count == (0 if replayed else 1)
    else:
        # Git/SVN は共通の純結果 builder を使い、ここで repository/remote を作らない。
        result = repository_effect_result(
            claimed, base_revision="original-revision", branch="skillmind/test",
            commit="written-revision", files={"src/handler.py": "保存済み"}, replayed=replayed,
        )
    graph.execution.verification_json = dict(result.verification)
    assert graph.tool.result_json is not None
    graph.tool.result_json["verification"] = dict(result.verification)
    graph.tool.result_json["replayed"] = result.replayed
    assert graph.execution.executed_at is not None
    assert graph.execution.before_ref is not None
    assert graph.execution.after_ref is not None
    before = EffectOperationsMixin._effect_evidence_row(
        run_id=graph.proposal.run_id, tool_call_id=graph.tool.id,
        evidence_ref=graph.execution.before_ref, draft=result.before,
        now=graph.execution.executed_at,
    )
    after = EffectOperationsMixin._effect_evidence_row(
        run_id=graph.proposal.run_id, tool_call_id=graph.tool.id,
        evidence_ref=graph.execution.after_ref, draft=result.after,
        now=graph.execution.executed_at,
    )
    assert await _invalid(replace(graph, before=before, after=after)) == frozenset()
