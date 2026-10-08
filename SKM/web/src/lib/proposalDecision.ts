import { ApiProblemError, type ChangeApprovalRecord, type ChangeProposalRecord, type RunDetailRecord } from '../api'

/** 承認送信後に保持する最小の原要求。再読取で草稿や新 Proposal に置き換えない。 */
export interface ProposalDecisionRequest {
  actorId: string
  projectId: string
  runId: string
  proposalId: string
  version: number
  checksum: string
  decision: 'APPROVED' | 'REJECTED'
  reason: string
  key: string
}

/** 保存成功と GET で観測した判断は異なる証拠なので別 phase にする。 */
export interface PendingProposalDecision {
  request: ProposalDecisionRequest
  phase: 'sending' | 'unknown' | 'confirmed' | 'observed' | 'conflict' | 'rejected'
  error: string | null
  approval: ChangeApprovalRecord | null
  recoveryUnavailable?: boolean
  accessDenied?: boolean
  rejectionStatus?: number
  editable?: boolean
}

/** 秘密情報を含めず、別 actor/Project/Run の保存済み要求を復元しない。 */
export interface ProposalDecisionScope { actorId: string; projectId: string; runId: string; proposalId: string }

/** Proposal の正確な version/checksum と判断を送信前に固定する。 */
export function freezeProposalDecision(scope: ProposalDecisionScope, proposal: ChangeProposalRecord,
  decision: ProposalDecisionRequest['decision'], reason: string, key: string): ProposalDecisionRequest {
  return Object.freeze({ ...scope, version: proposal.version, checksum: proposal.checksum, decision, reason: reason.trim(), key })
}

/** 400 系の明確な拒否以外は、契約不一致を含めて実行されなかったと推論しない。 */
export function proposalFailurePhase(error: unknown): 'unknown' | 'conflict' | 'rejected' {
  if (!(error instanceof ApiProblemError)) return 'unknown'
  if (error.status === 409) return 'conflict'
  return [400, 401, 403, 404, 410, 422].includes(error.status) ? 'rejected' : 'unknown'
}

/** GET に要求 key は無い。同版の承認を観測できても元要求の receipt とは呼ばない。 */
export function observeProposalDecision(detail: Pick<RunDetailRecord, 'project_id' | 'run_id' | 'approvals'>, request: ProposalDecisionRequest): {
  phase: 'unknown' | 'observed' | 'conflict'; approval: ChangeApprovalRecord | null
} {
  if (!sameId(detail.project_id, request.projectId) || !sameId(detail.run_id, request.runId)) {
    return { phase: 'unknown', approval: null }
  }
  const approvals = detail.approvals.filter((approval) => sameId(approval.run_id, request.runId)
    && sameId(approval.proposal_id, request.proposalId) && approval.proposal_version === request.version
    && approval.proposal_checksum === request.checksum)
  const match = approvals.find((approval) => approval.source === 'USER'
    && (!request.actorId || sameId(approval.actor_id ?? '', request.actorId))
    && approval.decision === request.decision && approval.reason === request.reason)
  if (match) return { phase: 'observed', approval: match }
  return { phase: approvals.length ? 'conflict' : 'unknown', approval: approvals[0] ?? null }
}

/** 版は key に含めず、未確認要求がある間の新しい版への判断も止める。 */
function storageKey(scope: ProposalDecisionScope): string {
  return `skillmind:proposal-decision:v1:${JSON.stringify([scope.actorId, scope.projectId, scope.runId, scope.proposalId].map((id) => id.toLowerCase()))}`
}

/** 同じタブの再読込でも送信を再実行せず、原要求を unknown として復元する。 */
export function restoreProposalDecision(scope: ProposalDecisionScope): { pending: PendingProposalDecision | null; unavailable: boolean; blocked: boolean } {
  if (!scope.actorId || typeof window === 'undefined') return { pending: null, unavailable: false, blocked: false }
  let raw: string | null
  try { raw = window.sessionStorage.getItem(storageKey(scope)) }
  catch { return { pending: null, unavailable: true, blocked: false } }
  if (!raw) return { pending: null, unavailable: false, blocked: false }
  try {
    const value: unknown = JSON.parse(raw)
    if (!isStoredRequest(value, scope)) return { pending: null, unavailable: true, blocked: true }
    return { pending: { request: Object.freeze(value), phase: 'unknown', error: null, approval: null }, unavailable: false, blocked: false }
  } catch { return { pending: null, unavailable: true, blocked: true } }
}

/** 保存失敗は現在の owner が原要求を保持し、利用者へ再読込の危険を伝える。 */
export function saveProposalDecision(request: ProposalDecisionRequest): boolean {
  if (!request.actorId || typeof window === 'undefined') return false
  try {
    const key = storageKey(request)
    const value = JSON.stringify(request)
    window.sessionStorage.setItem(key, value)
    return window.sessionStorage.getItem(key) === value
  } catch { return false }
}

/** 明確な未受理だけを保存記録から外す。別 key の記録は消さない。 */
export function removeRefusedProposalDecision(request: ProposalDecisionRequest): boolean {
  if (!request.actorId || typeof window === 'undefined') return true
  try {
    const key = storageKey(request)
    const raw = window.sessionStorage.getItem(key)
    if (!raw) return true
    const value: unknown = JSON.parse(raw)
    if (!isStoredRequest(value, request) || value.key !== request.key) return false
    window.sessionStorage.removeItem(key)
    return window.sessionStorage.getItem(key) === null
  } catch { return false }
}

/** 保存値は信用せず、scope と最小 request shape を照合する。 */
function isStoredRequest(value: unknown, scope: ProposalDecisionScope): value is ProposalDecisionRequest {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const record = value as Record<string, unknown>
  return (['actorId', 'projectId', 'runId', 'proposalId'] as const).every((key) => typeof record[key] === 'string' && sameId(record[key], scope[key]))
    && Number.isSafeInteger(record.version) && Number(record.version) > 0
    && typeof record.checksum === 'string' && record.checksum.length > 0
    && ['APPROVED', 'REJECTED'].includes(String(record.decision))
    && typeof record.reason === 'string' && record.reason.trim().length > 0 && record.reason.length <= 1000
    && typeof record.key === 'string' && record.key.length > 0 && record.key.length <= 256
}

/** 公開 UUID の表記差だけを同一と扱い、checksum や理由は変換しない。 */
function sameId(left: string, right: string): boolean { return left.toLowerCase() === right.toLowerCase() }
