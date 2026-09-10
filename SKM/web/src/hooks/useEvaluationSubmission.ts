import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { loadEvaluationSubmission, submitEvaluation, type CreateEvaluationInput,
  type EvaluationSubmissionReceipt } from '../api'
import { createIdempotencyKey } from '../lib/idempotency'
import { isNonNilUuid } from '../lib/validation'
import { EVALUATION_REQUEST_POLICY, evaluationAccessFailure, evaluationFailure, evaluationPayload,
  evaluationReadBlocked, freezeEvaluationSubmission, matchesEvaluationReceipt, type EvaluationFailure, type EvaluationScope,
  type PendingEvaluationSubmission } from '../lib/evaluationSubmission'
import { useResourceMutation, useResourceQuery, type SessionEnded } from './useResourceRequest'

/** Parent の actor/Session/Project/Run/Result key が原要求の寿命を決める。 */
export function useEvaluationSubmission({ scope, csrfToken, resultData, writable, accessFailure = null, onSaved, onSessionExpired }: {
  scope: EvaluationScope; csrfToken: string; resultData: unknown; writable: boolean
  accessFailure?: EvaluationFailure | null
  onSaved: (receipt: EvaluationSubmissionReceipt) => void; onSessionExpired: SessionEnded
}) {
  const [pending, setPending] = useState<PendingEvaluationSubmission | null>(null)
  const original = useRef<PendingEvaluationSubmission | null>(null)
  const [denied, setDenied] = useState<EvaluationFailure | null>(null)
  const deniedRef = useRef<EvaluationFailure | null>(null)
  const expiryNotified = useRef(false)
  const [confirmation, setConfirmation] = useState<PendingEvaluationSubmission | null>(null)
  const checking = useRef<PendingEvaluationSubmission | null>(null)
  const current = useRef({ scope, csrfToken, resultData, writable, accessFailure, onSaved })
  current.current = { scope, csrfToken, resultData, writable, accessFailure, onSaved }
  /** State 反映前にも別の submit/照合が割り込まないよう原要求を同期的に保護する。 */
  const publish = useCallback((value: PendingEvaluationSubmission | null) => {
    original.current = value; setPending(value)
  }, [])
  /** 参照拒否は再読取で無条件に消さず、unknown の履歴も取り消さない。 */
  const deny = useCallback((failure: EvaluationFailure) => {
    deniedRef.current = failure; setDenied((prior) => prior?.key === failure.key ? prior : failure)
  }, [])
  /** 共有 mutation は 401 で失効 callback を優先するため、原要求もここで閉じる。 */
  const sessionEnded = useCallback<SessionEnded>(() => {
    if (expiryNotified.current) return
    expiryNotified.current = true
    deny({ key: 'sessionExpired' })
    const value = original.current
    if (value && value.phase !== 'confirmed') publish({ ...value, phase: value.uncertain ? 'unknown' : 'refused', failure: { key: 'sessionExpired' } })
    onSessionExpired()
  }, [deny, publish, onSessionExpired])
  const mutation = useResourceMutation(sessionEnded, EVALUATION_REQUEST_POLICY)
  /** 期限/旧世代の判定を通った GET 拒否だけを同じ書込 gate に伝える。 */
  const observeFailure = useCallback((error: unknown) => {
    const failure = evaluationAccessFailure(evaluationFailure(error, false))
    if (failure) deny(failure)
  }, [deny])
  const loader = useCallback(async (signal: AbortSignal) => {
    if (!confirmation || checking.current !== confirmation) throw new DOMException('Obsolete request', 'AbortError')
    const request = confirmation.request
    const receipt = await loadEvaluationSubmission(request.scope.projectId, request.scope.runId, request.scope.resultId, request.key, signal)
    if (!matchesEvaluationReceipt(receipt, request)) throw new Error('Evaluation receipt does not match the original request')
    return receipt
  }, [confirmation])
  const query = useResourceQuery('evaluation-confirmation', loader, sessionEnded, EVALUATION_REQUEST_POLICY,
    confirmation !== null, observeFailure)

  useEffect(() => {
    if (!confirmation || checking.current !== confirmation || query.pending) return
    checking.current = null; setConfirmation(null)
    const value = original.current
    if (value?.request !== confirmation.request) return
    const refusal = deniedRef.current ?? current.current.accessFailure
    if (evaluationReadBlocked(refusal)) {
      publish({ ...value, phase: 'unknown', uncertain: true, failure: refusal })
    } else if (query.data && !query.failure) {
      publish({ ...value, phase: 'confirmed', failure: null, receipt: query.data })
      current.current.onSaved(query.data)
    } else publish({ ...value, phase: 'unknown', uncertain: true, failure: query.failure ?? { key: 'loadFailed' } })
  }, [confirmation, query.pending, query.data, query.failure, publish])

  /** 新規/原要求再送の両方に同じ同期 gate と厳密受付記録照合を用いる。 */
  function send(value: PendingEvaluationSubmission): void {
    const live = current.current
    if (!live.writable || live.accessFailure || deniedRef.current || checking.current || !live.csrfToken
      || !isNonNilUuid(live.scope.actorId) || original.current?.phase === 'sending' || value.request.body === null) return
    mutation.acknowledge()
    const accepted = mutation.submit(async (signal) => {
      const request = value.request
      const receipt = await submitEvaluation(request.scope.projectId, request.scope.runId, evaluationPayload(request), live.csrfToken, signal)
      if (!matchesEvaluationReceipt(receipt, request)) throw new Error('Evaluation receipt does not match the original request')
      return receipt
    }, (receipt) => {
      if (deniedRef.current || current.current.accessFailure) {
        publish({ ...value, phase: 'unknown', uncertain: true, failure: deniedRef.current ?? current.current.accessFailure })
        return
      }
      publish({ ...value, phase: 'confirmed', failure: null, receipt })
      current.current.onSaved(receipt)
    }, (failure) => {
      const refusal = evaluationAccessFailure(failure)
      if (refusal) deny(refusal)
      const uncertain = value.uncertain || failure.key === 'unknown' || failure.key === 'conflict'
      publish({ ...value, phase: uncertain ? 'unknown' : 'refused', uncertain, failure })
    })
    if (accepted) publish({ ...value, phase: 'sending', failure: null })
  }

  /** 明示された新規評価にだけ key を発行し、現在未決の原要求を上書きしない。 */
  function start(input: CreateEvaluationInput): void {
    if (original.current || !current.current.writable || !Object.values(current.current.scope).every(isNonNilUuid)) return
    const live = current.current
    send({ request: freezeEvaluationSubmission(live.scope, createIdempotencyKey(), live.resultData, input),
      phase: 'unknown', uncertain: false, failure: null, receipt: null })
  }

  /** 再送は保存前の原本文だけを使う。GET 404 を rollback の証拠にはしない。 */
  function resend(): void {
    const value = original.current
    if (value?.phase === 'unknown' && value.request.body !== null) send(value)
  }

  /** 明示操作でだけ原 key を GET し、初回保存になり得る POST とは分離する。 */
  function confirm(): void {
    const value = original.current
    if (!value || value.phase === 'sending' || value.phase === 'confirmed' || checking.current
      || evaluationReadBlocked(deniedRef.current ?? current.current.accessFailure)) return
    checking.current = value; setConfirmation(value)
    publish({ ...value, phase: 'checking', failure: null })
  }

  /** 新しい有効 Session は原 key を手入力して読めるが、旧草稿/送信を自動移行しない。 */
  function lookup(key: string): boolean {
    if (original.current || !isNonNilUuid(key) || !Object.values(current.current.scope).every(isNonNilUuid)) return false
    const live = current.current
    publish({ request: freezeEvaluationSubmission(live.scope, key, live.resultData, null), phase: 'unknown', uncertain: true, failure: null, receipt: null })
    confirm()
    return true
  }

  /** ローカル待機を中断してもサーバ側の撤回とは表示しない。 */
  function cancel(): void {
    if (original.current?.phase === 'sending') mutation.interrupt()
    else if (checking.current) {
      query.refresh()
      checking.current = null; setConfirmation(null)
      const value = original.current
      if (value) publish({ ...value, phase: 'unknown', uncertain: true, failure: { key: 'unknown' } })
    }
  }

  /** 確認済み、または先行 unknown の無い入力拒否だけを明示的に次の草稿へ戻す。 */
  function clear(): boolean {
    const value = original.current
    if (value?.phase !== 'confirmed' && !(value?.phase === 'refused' && !value.uncertain
      && ['invalidRequest', 'invalidRevision'].includes(value.failure?.key ?? ''))) return false
    mutation.acknowledge(); publish(null)
    return true
  }

  /** POST を一度も持たない手入力照会だけを閉じる。送信未知にはこの出口を与えない。 */
  function closeLookup(): void {
    if (original.current?.request.body !== null) return
    query.refresh(); checking.current = null; setConfirmation(null); publish(null)
  }

  /** Workspace と並行履歴の現在の拒否を共有し、旧 request の副作用を閉じる。 */
  const rejectAccess = useCallback((failure: EvaluationFailure) => {
    const refusal = evaluationAccessFailure(failure)
    if (!refusal) return
    deny(refusal)
    mutation.interrupt()
    if (evaluationReadBlocked(refusal) && checking.current) {
      query.refresh(); checking.current = null; setConfirmation(null)
      const value = original.current
      if (value && value.phase !== 'confirmed') publish({ ...value, phase: 'unknown', uncertain: true, failure: refusal })
    }
  }, [deny, mutation.interrupt, query.refresh, publish])
  useLayoutEffect(() => {
    if (accessFailure) rejectAccess(accessFailure)
  }, [accessFailure, rejectAccess])
  useLayoutEffect(() => () => { checking.current = null }, [])

  return { pending, start, resend, confirm, lookup, cancel, clear, closeLookup, rejectAccess,
    onReadSessionExpired: sessionEnded, denied: denied ?? accessFailure, busy: mutation.busy || confirmation !== null }
}
