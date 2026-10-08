import { useLayoutEffect, useRef, useState } from 'react'

import { ApiProblemError, decideChangeProposal, loadRunDetail, type ChangeProposalRecord } from '../api'
import { proposalMemoryKey, useProposalDecisionMemory } from '../components/ProposalDecisionOwner'
import { createIdempotencyKey } from '../lib/idempotency'
import { freezeProposalDecision, observeProposalDecision, proposalFailurePhase, removeRefusedProposalDecision, restoreProposalDecision,
  saveProposalDecision, type PendingProposalDecision, type ProposalDecisionScope } from '../lib/proposalDecision'
import type { SessionEnded } from './useResourceRequest'

/** 原承認を一度送る owner。復元・照合は GET だけで行い、外部 write を再送しない。 */
export function useProposalDecision({ scope, proposal, csrfToken, writable, onDecided, onSessionExpired }: {
  scope: ProposalDecisionScope; proposal: ChangeProposalRecord; csrfToken: string; writable: boolean
  onDecided?: () => void; onSessionExpired?: SessionEnded
}) {
  const memory = useProposalDecisionMemory()
  const memoryKey = proposalMemoryKey(scope)
  const [restored] = useState(() => {
    const cached = memory?.get(memoryKey)
    if (cached) return { pending: { ...cached, phase: cached.phase === 'sending' ? 'unknown' as const : cached.phase },
      unavailable: cached.recoveryUnavailable ?? false, blocked: false }
    return restoreProposalDecision(scope)
  })
  const [pending, setPending] = useState<PendingProposalDecision | null>(restored.pending)
  const original = useRef(pending)
  const [storageUnavailable, setStorageUnavailable] = useState(restored.unavailable)
  const [checking, setChecking] = useState(false)
  const [readFailed, setReadFailed] = useState(false)
  const [checked, setChecked] = useState(false)
  const [accessDenied, setAccessDenied] = useState(restored.pending?.accessDenied ?? false)
  const controller = useRef<AbortController | null>(null)
  const timeout = useRef<ReturnType<typeof setTimeout> | null>(null)
  const live = useRef({ writable, proposal, scope, csrfToken, onDecided, onSessionExpired })
  live.current = { writable, proposal, scope, csrfToken, onDecided, onSessionExpired }
  useLayoutEffect(() => () => {
    controller.current?.abort(); controller.current = null
    if (timeout.current !== null) clearTimeout(timeout.current)
  }, [])

  /** 同期 ref が React の再描画前にも次の判断を拒否する。 */
  function publish(value: PendingProposalDecision): void {
    original.current = value; memory?.set(memoryKey, value); setPending(value)
  }

  /** 初回だけ固定 key を送信する。通信中断・timeout・不正 success body は結果未知。 */
  async function start(decision: 'APPROVED' | 'REJECTED', reason: string): Promise<void> {
    const current = live.current
    if (original.current || controller.current || accessDenied || restored.blocked || !current.writable
      || current.proposal.status !== 'PENDING_APPROVAL' || !(Date.parse(current.proposal.expires_at) > Date.now())
      || !reason.trim()) return
    const request = freezeProposalDecision(current.scope, current.proposal, decision, reason, createIdempotencyKey())
    const value: PendingProposalDecision = { request, phase: 'sending', error: null, approval: null,
      recoveryUnavailable: !saveProposalDecision(request) }
    publish(value)
    setStorageUnavailable(Boolean(value.recoveryUnavailable))
    const active = new AbortController()
    controller.current = active
    const timer = setTimeout(() => {
      if (controller.current !== active) return
      controller.current = null; timeout.current = null; active.abort()
      publish({ ...value, phase: 'unknown', error: null })
    }, 30_000)
    timeout.current = timer
    try {
      const receipt = await decideChangeProposal(request.projectId, request.runId, current.proposal,
        request.decision, request.reason, request.key, current.csrfToken, active.signal)
      if (controller.current !== active) return
      // 型検証済みでも別 Proposal の応答で送信成功を確定しない。
      const observation = observeProposalDecision({ project_id: request.projectId, run_id: request.runId,
        approvals: [receipt.approval] }, request)
      if (receipt.proposal.proposal_id.toLowerCase() !== request.proposalId.toLowerCase()
        || receipt.proposal.project_id.toLowerCase() !== request.projectId.toLowerCase()
        || receipt.proposal.run_id.toLowerCase() !== request.runId.toLowerCase()
        || receipt.proposal.version !== request.version || receipt.proposal.checksum !== request.checksum
        || observation.phase !== 'observed') throw new Error('Proposal decision did not match the original request')
      publish({ ...value, phase: 'confirmed', approval: receipt.approval })
      live.current.onDecided?.()
    } catch (error: unknown) {
      if (controller.current !== active) return
      const rejectionStatus = error instanceof ApiProblemError && [400, 422].includes(error.status) ? error.status : undefined
      if (rejectionStatus && !removeRefusedProposalDecision(request)) setStorageUnavailable(true)
      publish({ ...value, phase: proposalFailurePhase(error), rejectionStatus,
        accessDenied: error instanceof ApiProblemError && [401, 403, 404].includes(error.status), error: error instanceof Error ? error.message : null })
      if (error instanceof ApiProblemError && [401, 403, 404].includes(error.status)) {
        setAccessDenied(true)
        if (error.status === 401) live.current.onSessionExpired?.()
      }
    } finally {
      clearTimeout(timer)
      if (controller.current === active) { controller.current = null; timeout.current = null }
    }
  }

  /** 原 Proposal version/checksum を GET の監査記録へ照合するだけで、再送信しない。 */
  async function check(): Promise<void> {
    const value = original.current
    if (!value || controller.current || accessDenied) return
    const active = new AbortController()
    controller.current = active
    setChecking(true); setReadFailed(false)
    const timer = setTimeout(() => {
      if (controller.current !== active) return
      controller.current = null; timeout.current = null; active.abort()
      setChecking(false); setReadFailed(true)
    }, 30_000)
    timeout.current = timer
    try {
      const detail = await loadRunDetail(value.request.projectId, value.request.runId, active.signal)
      if (controller.current !== active) return
      const observation = observeProposalDecision(detail, value.request)
      const latest = detail.change_proposals.find((item) => item.proposal_id.toLowerCase() === value.request.proposalId.toLowerCase())
      const knownRefusal = value.phase === 'rejected' && [400, 422].includes(value.rejectionStatus ?? 0)
      const editable = knownRefusal && observation.phase === 'unknown'
        && detail.project_id.toLowerCase() === value.request.projectId.toLowerCase()
        && detail.run_id.toLowerCase() === value.request.runId.toLowerCase()
        && latest?.version === value.request.version && latest.checksum === value.request.checksum
        && latest.status === 'PENDING_APPROVAL' && Date.parse(latest.expires_at) > Date.now()
      publish({ ...value, ...observation, phase: knownRefusal && observation.phase === 'unknown' ? 'rejected' : observation.phase,
        editable, error: knownRefusal ? value.error : null })
      setChecked(true)
    } catch (error: unknown) {
      if (controller.current !== active) return
      setReadFailed(true)
      if (error instanceof ApiProblemError && [401, 403, 404].includes(error.status)) {
        publish({ ...value, accessDenied: true })
        setAccessDenied(true)
        if (error.status === 401) live.current.onSessionExpired?.()
      }
    } finally {
      clearTimeout(timer)
      if (controller.current === active) { controller.current = null; timeout.current = null; setChecking(false) }
    }
  }
  /** 明確な未受理と最新 GET の未決状態を確認した後、明示操作だけで草稿へ戻す。 */
  function editRejected(): void {
    const value = original.current
    const current = live.current
    if (controller.current || accessDenied || !current.writable || !value?.editable || value.phase !== 'rejected'
      || ![400, 422].includes(value.rejectionStatus ?? 0) || current.proposal.version !== value.request.version
      || current.proposal.checksum !== value.request.checksum || current.proposal.status !== 'PENDING_APPROVAL'
      || !(Date.parse(current.proposal.expires_at) > Date.now())) return
    const removed = removeRefusedProposalDecision(value.request)
    memory?.delete(memoryKey); original.current = null; setPending(null)
    setStorageUnavailable(!removed); setChecked(false); setReadFailed(false)
  }
  return { pending, start, check, editRejected, checking, readFailed, checked, storageUnavailable, accessDenied,
    locked: Boolean(pending) || restored.blocked || accessDenied || !writable }
}
