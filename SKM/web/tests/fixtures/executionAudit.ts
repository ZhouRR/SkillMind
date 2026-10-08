import type { ChangeApprovalRecord, ChangeProposalRecord, ProposalDecisionRecord, RunDetailRecord, ToolCallDetail } from '../../src/api'
import { INTERACTION_SCOPE, interactionDetail } from './interaction'

/** 承認 UI 回帰用の架空 owner。外部 write を持つ実資源は使わない。 */
export const PROPOSAL_SCOPE = { ...INTERACTION_SCOPE, proposalId: '00000000-0000-4000-8000-000000000120' }

/** 原 version/checksum と長い target を固定した未承認提案。 */
export function proposalFixture(): ChangeProposalRecord {
  return { proposal_id: PROPOSAL_SCOPE.proposalId, proposal_ref: 'cp_fixture', project_id: PROPOSAL_SCOPE.projectId,
    run_id: PROPOSAL_SCOPE.runId, run_segment_id: '00000000-0000-4000-8000-000000000050',
    agent_session_id: '00000000-0000-4000-8000-000000000060', target_binding_id: '00000000-0000-4000-8000-000000000122',
    integration_id: '00000000-0000-4000-8000-000000000123', effect_intent_key: 'fixture-update', capability_version: 'repository.write/v1',
    operation: 'commit', target: { display: `fixture/${'long-path'.repeat(30)}/target` }, summary: 'Review the exact original file',
    changes: [{ path: '/files/src/fixture.ts', action: 'SET', value: 'export const fixture = true\n' }],
    precondition: { revision: 'original-revision' }, evidence_refs: [], risk_level: 'MEDIUM', reversible: true,
    rollback: {}, verification: {}, status: 'PENDING_APPROVAL', version: 4, checksum: `sha256:${'b'.repeat(64)}`,
    expires_at: '2099-12-31T12:30:45Z', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' }
}

/** 元要求と一致する判断。要求 key が無いことも公開 API 契約どおりに保つ。 */
export function approvalFixture(decision: 'APPROVED' | 'REJECTED' = 'APPROVED', reason = 'Original reason'): ChangeApprovalRecord {
  const proposal = proposalFixture()
  return { approval_id: '00000000-0000-4000-8000-000000000124', proposal_id: proposal.proposal_id, run_id: proposal.run_id,
    source: 'USER', decision, actor_id: PROPOSAL_SCOPE.actorId, preauthorization_id: null, proposal_version: proposal.version,
    proposal_checksum: proposal.checksum, reason, created_at: '2026-01-01T00:01:00Z' }
}

/** 成功 POST の正式 response。GET の監査観測とは証拠を分ける。 */
export function proposalReceipt(decision: 'APPROVED' | 'REJECTED' = 'APPROVED', reason = 'Original reason'): ProposalDecisionRecord {
  return { proposal: { ...proposalFixture(), status: decision }, approval: approvalFixture(decision, reason), effect_execution: null,
    run_status: 'QUEUED', idempotent_replay: false }
}

/** 承認だけを含む detail にして別の request hook の副作用を混ぜない。 */
export function executionDetail(): RunDetailRecord {
  return { ...interactionDetail(), interactions: [], status: 'WAITING_FOR_APPROVAL', change_proposals: [proposalFixture()] }
}

/** 状態文字列と長い原引数を監査する合成 tool entry。 */
export function toolFixture(status = 'FAILED'): ToolCallDetail {
  return { tool_call_id: '00000000-0000-4000-8000-000000000130', run_attempt_id: '00000000-0000-4000-8000-000000000131',
    agent_session_id: '00000000-0000-4000-8000-000000000060', tool_name: 'fixture.read', capability: 'fixture.read/v1',
    provider: 'fixture', arguments_summary: { path: 'long-argument'.repeat(50) }, status, duration_ms: 0, created_at: '2026-01-01T00:00:00Z' }
}
