import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiProblemError, confirmReconciliation, loadLatestReconciliation, requestReconciliation, type ReconciliationScope } from '../api'
import { useResourceMutation, useResourceQuery, type ResourceRequestPolicy, type SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { createIdempotencyKey } from '../lib/idempotency'
import { formatLocalTimestamp } from '../lib/presentation'
import { isNonNilUuid, sameUuid } from '../lib/validation'

/** サーバー本文を表示せず、再読可能な拒否/未知だけを区別する。 */
interface Failure { key: 'sessionExpired' | 'denied' | 'unavailable' | 'conflict' | 'unknown' }
const POLICY: ResourceRequestPolicy<Failure> = {
  classify: (error) => ({ key: error instanceof ApiProblemError
    ? error.status === 401 ? 'sessionExpired' : error.status === 403 || error.status === 404 ? 'denied'
      : error.code === 'reconciliation_conflict' ? 'conflict'
        : error.code === 'reconciliation_unavailable' ? 'unavailable' : 'unknown'
    : 'unknown' }),
  readTimeout: { key: 'unknown' }, writeTimeout: { key: 'unknown' }, blocks: () => true,
}

/** 親は actor/会話/Project/Run/Effect を key にして再作成し、旧 transport を閉じる。 */
export function EffectReconciliation({ scope, actorId, csrfToken, onSessionExpired }: {
  scope: ReconciliationScope; actorId: string; csrfToken: string; onSessionExpired: SessionEnded
}) {
  const labels = useMessages().runResult.reconciliation
  // 原 UUID と非機密の帰属だけを tab に保存する。会話を変えても不明な要求を勝手に捨てない。
  const storageKey = `skillmind:effect-reconciliation:v1:${JSON.stringify([actorId, scope.projectId, scope.runId, scope.effectId].map((s) => s.toLowerCase()))}`
  const [pendingId, setPendingId] = useState<string | null>(null)
  const pending = useRef<string | null>(null)
  const [initialized, setInitialized] = useState(false)
  const [storageError, setStorageError] = useState(false)
  const restore = useCallback(() => {
    try {
      const value = sessionStorage.getItem(storageKey)
      if (value !== null && !isNonNilUuid(value)) throw new Error('Invalid saved request')
      pending.current = value
      setPendingId(value)
      setStorageError(false)
    } catch { setStorageError(true) }
    setInitialized(true)
  }, [storageKey])
  useEffect(restore, [restore])
  const loader = useCallback(async (signal: AbortSignal) => {
    if (pendingId) {
      try { return { record: await confirmReconciliation(scope, pendingId, signal), missing: false } }
      catch (error) {
        if (error instanceof ApiProblemError && error.code === 'reconciliation_not_found') {
          return { record: await loadLatestReconciliation(scope, signal), missing: true }
        }
        throw error
      }
    }
    return { record: await loadLatestReconciliation(scope, signal), missing: false }
  }, [scope.projectId, scope.runId, scope.effectId, pendingId])
  const query = useResourceQuery(`${storageKey}:${pendingId ?? ''}`, loader, onSessionExpired, POLICY, initialized)
  const mutation = useResourceMutation(onSessionExpired, POLICY)
  const record = query.data?.record
  const active = record?.status === 'QUEUED' || record?.status === 'RUNNING'

  /** 受理の存在を確認した後だけ browser の不明要求を閉じる。 */
  const acknowledge = useCallback((requestId: string) => {
    if (pending.current === null || !sameUuid(pending.current, requestId)) return
    try {
      sessionStorage.removeItem(storageKey)
      pending.current = null
      setPendingId(null)
      setStorageError(false)
    } catch { setStorageError(true) }
  }, [storageKey])
  useEffect(() => {
    if (!query.pending && !query.failure && record) {
      acknowledge(record.request_id)
      if (!pendingId || sameUuid(pendingId, record.request_id)) mutation.acknowledge()
    }
  }, [query.pending, query.failure, record, acknowledge, pendingId, mutation.acknowledge, mutation.busy, mutation.failure])
  useEffect(() => {
    if (!active || query.pending || query.failure || mutation.busy) return
    const timer = window.setTimeout(query.refresh, 2000)
    return () => window.clearTimeout(timer)
  }, [active, query.pending, query.failure, mutation.busy, query.refresh, query.revision])

  /** 送信前に原 UUID を保存し、unknown/再読未検出でも同じ要求でだけ再試行する。 */
  function start(): void {
    if (mutation.busy || query.pending || active || storageError || !initialized) return
    let id = pending.current
    if (id === null) {
      try {
        id = createIdempotencyKey()
        sessionStorage.setItem(storageKey, id)
        pending.current = id
        setPendingId(id)
      } catch { setStorageError(true); return }
    }
    const requestId = id
    mutation.acknowledge()
    mutation.submit((signal) => requestReconciliation(scope, requestId, csrfToken, signal), (value) => {
      acknowledge(value.request_id)
      query.refresh()
    }, () => query.refresh())
  }

  return <div className="reconciliationPanel">
    <p className="hint">{labels.hint}</p>
    {query.pending && !record ? <p role="status">{labels.loading}</p> : record ? <>
      <p><strong>{labels.latest}: {labels.statuses[record.status]}</strong> · {formatLocalTimestamp(record.created_at)}</p>
      {record.observation_status && <p role="status">{labels.observations[record.observation_status]}</p>}
      {record.kind === 'DOCUMENT_OBJECT' && record.observation_status === 'CONFIRMED' && <p className="hint">{labels.objectOnly}</p>}
      {record.observed_at && <p className="hint">{labels.observedAt}: {formatLocalTimestamp(record.observed_at)}</p>}
      {record.error_code && <p className="hint">{labels.errors[record.error_code]}</p>}
    </> : !query.failure && <p>{pendingId ? labels.pending : labels.empty}</p>}
    {pendingId && <p className="hint">{labels.pending} <code>{pendingId}</code></p>}
    {storageError && <p className="error" role="alert">{labels.storageError}</p>}
    {(query.failure || mutation.failure) && <p className="error" role="alert">{labels.failures[(query.failure ?? mutation.failure)!.key]}</p>}
    <div className="resultActions">
      <button className="secondaryButton compactButton" type="button"
        disabled={mutation.busy || query.pending || active || storageError || !initialized || !!query.failure}
        onClick={start}>{mutation.busy ? labels.sending : pendingId ? labels.retry : labels.start}</button>
      <button className="secondaryButton compactButton" type="button" disabled={query.pending || mutation.busy}
        onClick={() => { if (storageError) restore(); query.refresh() }}>{labels.refresh}</button>
    </div>
  </div>
}
