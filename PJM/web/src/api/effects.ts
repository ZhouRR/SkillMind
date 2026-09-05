import { API_BASE, hasStrings, isRecord, parseItemList, requestApiJson } from './http'

const PROPOSAL_STATUSES = new Set([
  'DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'APPLYING', 'APPLIED', 'REJECTED', 'STALE', 'FAILED',
])
const EFFECT_STATUSES = new Set([
  'REQUESTED', 'LEASED', 'APPLYING', 'APPLIED', 'STALE', 'FAILED', 'VERIFICATION_FAILED',
])

/** Agent が作成した未授权の外部変更 Proposal。 */
export interface ChangeProposalRecord {
  proposal_id: string
  proposal_ref: string
  project_id: string
  run_id: string
  run_segment_id: string
  agent_session_id: string
  target_binding_id: string
  integration_id: string
  effect_intent_key: string
  capability_version: string
  operation: string
  target: Record<string, unknown>
  summary: string
  changes: Array<Record<string, unknown>>
  precondition: Record<string, unknown>
  evidence_refs: string[]
  risk_level: 'LOW' | 'MEDIUM' | 'HIGH'
  reversible: boolean
  rollback: Record<string, unknown>
  verification: Record<string, unknown>
  status: 'DRAFT' | 'PENDING_APPROVAL' | 'APPROVED' | 'APPLYING' | 'APPLIED' | 'REJECTED' | 'STALE' | 'FAILED'
  version: number
  checksum: string
  expires_at: string
  created_at: string
  updated_at: string
}

/** Exact Proposal version への user/preauthorization 判断。 */
export interface ChangeApprovalRecord {
  approval_id: string
  proposal_id: string
  run_id: string
  source: 'USER' | 'PREAUTHORIZATION'
  decision: 'APPROVED' | 'REJECTED'
  actor_id: string | null
  preauthorization_id: string | null
  proposal_version: number
  proposal_checksum: string
  reason: string
  created_at: string
}

/** Approval 後の Provider apply と read-back verification 監査。 */
export interface EffectExecutionRecord {
  effect_execution_id: string
  proposal_id: string
  run_id: string
  approval_id: string
  tool_call_id: string | null
  status: 'REQUESTED' | 'LEASED' | 'APPLYING' | 'APPLIED' | 'STALE' | 'FAILED' | 'VERIFICATION_FAILED'
  provider: string
  provider_version: string
  before_ref: string | null
  after_ref: string | null
  verification: Record<string, unknown>
  error: Record<string, unknown> | null
  attempt_no: number
  executed_at: string | null
  created_at: string
  updated_at: string
}

/** User decision 後の Proposal aggregate snapshot。 */
export interface ProposalDecisionRecord {
  proposal: ChangeProposalRecord
  approval: ChangeApprovalRecord
  effect_execution: EffectExecutionRecord | null
  run_status: string
  idempotent_replay: boolean
}

/** ADMIN が構成する LOW-only exact effect policy。 */
export interface EffectPreauthorizationRecord {
  preauthorization_id: string
  project_id: string
  integration_id: string
  capability_version: string
  operation: string
  max_risk_level: 'LOW'
  scope: Record<string, unknown>
  status: 'ACTIVE' | 'DISABLED'
  policy_version: number
  created_by: string
  expires_at: string | null
  disabled_at: string | null
  created_at: string
  updated_at: string
}

/** LOW-only exact scope policy 作成 request。 */
export interface CreateEffectPreauthorizationInput {
  integration_id: string
  capability_version: string
  operation: string
  risk_level: 'LOW'
  scope: Record<string, unknown>
  expires_at: string | null
}

/** 表示中の exact Proposal version/checksum を批准または拒否する。 */
export async function decideChangeProposal(
  projectId: string,
  runId: string,
  proposal: ChangeProposalRecord,
  decision: 'APPROVED' | 'REJECTED',
  reason: string,
  idempotencyKey: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProposalDecisionRecord> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}`
      + `/proposals/${encodeURIComponent(proposal.proposal_id)}/decision`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({
        decision,
        proposal_version: proposal.version,
        proposal_checksum: proposal.checksum,
        reason,
      }),
      signal,
    },
  )
  if (
    !isRecord(value)
    || !isChangeProposal(value.proposal)
    || !isChangeApproval(value.approval)
    || !(value.effect_execution === null || isEffectExecution(value.effect_execution))
    || typeof value.run_status !== 'string'
    || typeof value.idempotent_replay !== 'boolean'
  ) throw new Error('Proposal decision did not match its contract')
  return value as unknown as ProposalDecisionRecord
}

/** Project の preauthorization policy を一覧する。 */
export async function loadEffectPreauthorizations(
  projectId: string,
  signal?: AbortSignal,
): Promise<EffectPreauthorizationRecord[]> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/effect-preauthorizations`,
    { signal },
  )
  return parseItemList(
    value,
    'items',
    isPreauthorization,
    'Effect preauthorization list did not match its contract',
  )
}

/** ADMIN が exact LOW-risk preauthorization を作成する。 */
export async function createEffectPreauthorization(
  projectId: string,
  input: CreateEffectPreauthorizationInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<EffectPreauthorizationRecord> {
  return parsePreauthorization(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/effect-preauthorizations`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input),
      signal,
    },
  ))
}

/** Optimistic policy version を指定して preauthorization を無効化する。 */
export async function disableEffectPreauthorization(
  projectId: string,
  preauthorizationId: string,
  expectedPolicyVersion: number,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<EffectPreauthorizationRecord> {
  return parsePreauthorization(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/effect-preauthorizations/`
      + `${encodeURIComponent(preauthorizationId)}/disable`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ expected_policy_version: expectedPolicyVersion }),
      signal,
    },
  ))
}

/** Run detail validator と共有する ChangeProposal 公開 shape 判定。 */
export function isChangeProposal(value: unknown): value is ChangeProposalRecord {
  return isRecord(value)
    && hasStrings(value, [
      'proposal_id', 'proposal_ref', 'project_id', 'run_id', 'run_segment_id',
      'agent_session_id', 'target_binding_id', 'integration_id', 'effect_intent_key',
      'capability_version', 'operation', 'summary', 'risk_level', 'status', 'checksum',
      'expires_at', 'created_at', 'updated_at',
    ])
    && isRecord(value.target)
    && Array.isArray(value.changes)
    && value.changes.every(isRecord)
    && isRecord(value.precondition)
    && Array.isArray(value.evidence_refs)
    && value.evidence_refs.every((item) => typeof item === 'string')
    && typeof value.reversible === 'boolean'
    && isRecord(value.rollback)
    && isRecord(value.verification)
    && ['LOW', 'MEDIUM', 'HIGH'].includes(value.risk_level as string)
    && PROPOSAL_STATUSES.has(value.status as string)
    && Number.isInteger(value.version)
}

/** Run detail validator と共有する ChangeApproval 公開 shape 判定。 */
export function isChangeApproval(value: unknown): value is ChangeApprovalRecord {
  return isRecord(value)
    && hasStrings(value, [
      'approval_id', 'proposal_id', 'run_id', 'source', 'decision',
      'proposal_checksum', 'reason', 'created_at',
    ])
    && (typeof value.actor_id === 'string' || value.actor_id === null)
    && (typeof value.preauthorization_id === 'string' || value.preauthorization_id === null)
    && (value.source === 'USER' || value.source === 'PREAUTHORIZATION')
    && (value.decision === 'APPROVED' || value.decision === 'REJECTED')
    && Number.isInteger(value.proposal_version)
}

/** Run detail validator と共有する EffectExecution 公開 shape 判定。 */
export function isEffectExecution(value: unknown): value is EffectExecutionRecord {
  return isRecord(value)
    && hasStrings(value, [
      'effect_execution_id', 'proposal_id', 'run_id', 'approval_id', 'status',
      'provider', 'provider_version', 'created_at', 'updated_at',
    ])
    && (typeof value.tool_call_id === 'string' || value.tool_call_id === null)
    && (typeof value.before_ref === 'string' || value.before_ref === null)
    && (typeof value.after_ref === 'string' || value.after_ref === null)
    && EFFECT_STATUSES.has(value.status as string)
    && isRecord(value.verification)
    && (isRecord(value.error) || value.error === null)
    && Number.isInteger(value.attempt_no)
    && (typeof value.executed_at === 'string' || value.executed_at === null)
}

/** Unknown JSON を EffectPreauthorization 公開 shape へ制限する。 */
function parsePreauthorization(value: unknown): EffectPreauthorizationRecord {
  if (!isPreauthorization(value)) {
    throw new Error('Effect preauthorization did not match its contract')
  }
  return value
}

/** EffectPreauthorization の exact policy shape を検証する。 */
function isPreauthorization(value: unknown): value is EffectPreauthorizationRecord {
  return isRecord(value)
    && hasStrings(value, [
      'preauthorization_id', 'project_id', 'integration_id', 'capability_version',
      'operation', 'max_risk_level', 'status', 'created_by', 'created_at', 'updated_at',
    ])
    && value.max_risk_level === 'LOW'
    && (value.status === 'ACTIVE' || value.status === 'DISABLED')
    && isRecord(value.scope)
    && Number.isInteger(value.policy_version)
    && (typeof value.expires_at === 'string' || value.expires_at === null)
    && (typeof value.disabled_at === 'string' || value.disabled_at === null)
}
