import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import {
  ApiProblemError, loadEffectPreauthorizations, loadIntegrations, loadProjectTasks, loadResourceBindings,
  loadSecretReferences, type EffectPreauthorizationRecord, type IntegrationRecord,
  type PublishedTaskRecord, type ResourceBindingRecord, type SecretReferenceRecord,
} from '../api'
import { useMessages } from '../i18n'
import { apiErrorMessage } from '../lib/apiFeedback'
import { RESOURCE_REQUEST_TIMEOUT_MS } from './useResourceRequest'

/** Task は独立して読み、補助 catalog の失敗で資源の事実を消さない。 */
interface ResourceCatalog {
  secrets: SecretReferenceRecord[]
  integrations: IntegrationRecord[]
  bindings: ResourceBindingRecord[]
  policies: EffectPreauthorizationRecord[]
}

const EMPTY_CATALOG: ResourceCatalog = { secrets: [], integrations: [], bindings: [], policies: [] }

/** 秘密値・送信関数は保存せず、同じ画面へ戻っても未確認の送信を知らせる。 */
interface UnconfirmedOperation { key: string; label: string }
let currentSession = ''
let sessionOperations = new Map<string, UnconfirmedOperation>()

/** Session 交替で旧 map を外し、別 actor に操作名や門禁を引き継がない。 */
function operationsForSession(session: string): Map<string, UnconfirmedOperation> {
  if (currentSession !== session) { currentSession = session; sessionOperations = new Map() }
  return sessionOperations
}

/** 管理画面の読取と単一操作の所有権を持つ。停止・期限超過は書込の取消確認ではない。 */
export function useResourceAdministration(projectId: string, deferredFeaturesEnabled: boolean, session: string) {
  const unconfirmedByProject = operationsForSession(session)
  const messages = useMessages()
  const [catalog, setCatalog] = useState<{ projectId: string; session: string; value: ResourceCatalog | null }>({ projectId, session, value: null })
  const [tasksState, setTasksState] = useState<{ projectId: string; session: string; status: 'loading' | 'ready' | 'error'; tasks: PublishedTaskRecord[] }>({ projectId, session, status: 'loading', tasks: [] })
  const [revision, setRevision] = useState(0)
  const [taskRevision, setTaskRevision] = useState(0)
  const [loading, setLoading] = useState(Boolean(projectId))
  // 原失敗を保持し、初回言語取得・言語切替でも読取を再送せず現在の catalog で表示する。
  const [loadFailure, setLoadFailure] = useState<{ kind: 'timeout' } | { kind: 'request'; error: unknown } | null>(null)
  const loadError = loadFailure?.kind === 'timeout' ? messages.resourcesAudit.readTimeout
    : loadFailure ? apiErrorMessage(loadFailure.error, messages.resources.loadFailed, messages) : null
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [, setUnconfirmed] = useState(unconfirmedByProject.get(projectId) ?? null)
  const [reviewed, setReviewed] = useState(false)
  const loadRequest = useRef<AbortController | null>(null)
  const actionRequest = useRef<AbortController | null>(null)
  const interruptRequest = useRef<((notify: boolean) => void) | null>(null)
  const owner = useRef(projectId)
  owner.current = projectId
  const mounted = useRef(false)
  const catalogReady = useRef(false)
  const reviewedRef = useRef(false)

  useLayoutEffect(() => {
    mounted.current = true
    catalogReady.current = false; reviewedRef.current = false
    setBusy(null); setError(null); setLoadFailure(null); setReviewed(false)
    setUnconfirmed(unconfirmedByProject.get(projectId) ?? null)
    return () => {
      mounted.current = false
      loadRequest.current?.abort()
      interruptRequest.current?.(false)
    }
  }, [projectId, session])

  useEffect(() => {
    if (!projectId) { setLoading(false); return }
    const controller = new AbortController()
    loadRequest.current = controller
    setLoading(true); setLoadFailure(null); setReviewed(false)
    catalogReady.current = false; reviewedRef.current = false
    const operationAtStart = unconfirmedByProject.get(projectId)
    let settled = false
    const expiresAt = performance.now() + RESOURCE_REQUEST_TIMEOUT_MS
    const current = (): boolean => !settled && mounted.current && owner.current === projectId
      && currentSession === session && loadRequest.current === controller && !controller.signal.aborted
    const expire = (): void => {
      if (!current()) return
      settled = true; controller.abort(); setLoading(false); setLoadFailure({ kind: 'timeout' })
    }
    const timer = window.setTimeout(expire, RESOURCE_REQUEST_TIMEOUT_MS)
    void Promise.all([
      loadSecretReferences(projectId, controller.signal),
      loadIntegrations(projectId, controller.signal),
      loadResourceBindings(projectId, controller.signal),
      deferredFeaturesEnabled ? loadEffectPreauthorizations(projectId, controller.signal) : Promise.resolve([]),
    ]).then(([secrets, integrations, bindings, policies]) => {
      if (!current()) return
      if (performance.now() >= expiresAt) { expire(); return }
      setCatalog({ projectId, session, value: { secrets, integrations, bindings, policies } })
      catalogReady.current = true
      reviewedRef.current = unconfirmedByProject.get(projectId) === operationAtStart
      setReviewed(reviewedRef.current)
    }).catch((caught: unknown) => {
      if (current()) setLoadFailure({ kind: 'request', error: caught })
    }).finally(() => {
      if (current()) { setLoading(false); settled = true }
      window.clearTimeout(timer)
    })
    return () => { settled = true; window.clearTimeout(timer); controller.abort() }
  }, [projectId, revision, deferredFeaturesEnabled, session])

  useEffect(() => {
    if (!projectId) return
    const controller = new AbortController()
    let settled = false
    setTasksState({ projectId, session, status: 'loading', tasks: [] })
    const expiresAt = performance.now() + RESOURCE_REQUEST_TIMEOUT_MS
    const current = (): boolean => !settled && mounted.current && owner.current === projectId && currentSession === session && !controller.signal.aborted
    const fail = (): void => {
      if (!current()) return
      settled = true; controller.abort(); setTasksState({ projectId, session, status: 'error', tasks: [] })
    }
    const timer = window.setTimeout(fail, RESOURCE_REQUEST_TIMEOUT_MS)
    void loadProjectTasks(projectId, controller.signal).then((value) => {
      if (!current()) return
      if (performance.now() >= expiresAt) { fail(); return }
      settled = true; setTasksState({ projectId, session, status: 'ready', tasks: value.tasks })
    }).catch(fail).finally(() => window.clearTimeout(timer))
    return () => { settled = true; window.clearTimeout(timer); controller.abort() }
  }, [projectId, taskRevision, session])

  /** 読取だけを再発行する。既存 write の再送・成功推測はしない。 */
  function refresh(): void {
    loadRequest.current?.abort()
    catalogReady.current = false; reviewedRef.current = false
    setReviewed(false); setRevision((value) => value + 1)
  }

  /** 同 tick の二重送信、未確認結果を残した新送信、旧 Project の callback を拒否する。 */
  function perform(
    key: string, action: (signal: AbortSignal) => Promise<unknown>, kind: 'read' | 'write' = 'write', label = key,
  ): Promise<boolean> {
    if (!mounted.current || owner.current !== projectId || actionRequest.current !== null
      || currentSession !== session || (kind === 'write' && (!catalogReady.current || unconfirmedByProject.has(projectId)))) return Promise.resolve(false)
    const controller = new AbortController()
    actionRequest.current = controller
    setBusy(key); setError(null)
    const expiresAt = performance.now() + RESOURCE_REQUEST_TIMEOUT_MS
    return new Promise((resolve) => {
      let settled = false
      const current = (): boolean => !settled && mounted.current && owner.current === projectId
        && currentSession === session && actionRequest.current === controller && !controller.signal.aborted
      /** 送信待機だけを閉じる。transport が Abort を無視しても古い結果は採用しない。 */
      const finish = (success: boolean, unknown: boolean, notify: boolean): void => {
        if (settled) return
        settled = true; window.clearTimeout(timer)
        if (unknown) {
          const operation = { key, label }
          unconfirmedByProject.set(projectId, operation)
          if (notify) { reviewedRef.current = false; setUnconfirmed(operation); setReviewed(false) }
        }
        if (actionRequest.current === controller) { actionRequest.current = null; interruptRequest.current = null }
        controller.abort()
        if (notify) setBusy(null)
        resolve(success)
      }
      const interrupt = (notify: boolean, timedOut = false): void => {
        if (kind === 'read' && notify && timedOut) setError(messages.resourcesAudit.readTimeout)
        finish(false, kind === 'write', notify)
      }
      interruptRequest.current = interrupt
      const timer = window.setTimeout(() => interrupt(current(), true), RESOURCE_REQUEST_TIMEOUT_MS)
      void Promise.resolve().then(() => {
        if (!current()) { finish(false, false, false); return }
        if (performance.now() >= expiresAt) { interrupt(true, true); return }
        return action(controller.signal)
      }).then(() => {
        if (!current()) return
        if (performance.now() >= expiresAt) { interrupt(true, true); return }
        finish(true, false, true)
        if (kind === 'write') refresh()
      }).catch((caught: unknown) => {
        if (!current()) return
        if (performance.now() >= expiresAt) { interrupt(true, true); return }
        // 4xx の明示拒否だけが再編集可能。5xx・通信・成功応答の契約違反は結果不明。
        const unknown = kind === 'write' && !(caught instanceof ApiProblemError
          && caught.status >= 400 && caught.status < 500 && caught.status !== 408)
        setError(apiErrorMessage(caught, kind === 'read' ? messages.resources.loadFailed : messages.resources.mutationFailed, messages))
        finish(false, unknown, true)
      })
    })
  }

  /** 人による最新状態の確認を必要とし、単なる refresh では再送禁止を解除しない。 */
  function acknowledge(): boolean {
    if (!reviewedRef.current || !reviewed || loading || loadError || actionRequest.current || currentSession !== session) return false
    reviewedRef.current = false
    unconfirmedByProject.delete(projectId); setUnconfirmed(null); setError(null); setReviewed(false)
    return true
  }

  /** 接続保存の中間成功を保持する。後段が既知拒否でも同じ Secret を再利用できる。 */
  function recordSecret(secret: SecretReferenceRecord): void {
    if (!mounted.current || owner.current !== projectId || currentSession !== session || secret.project_id !== projectId) return
    setCatalog((current) => current.projectId !== projectId || current.session !== session || current.value === null ? current : ({ ...current,
      value: { ...current.value, secrets: [...current.value.secrets.filter(
        (item) => item.secret_reference_id !== secret.secret_reference_id), secret] },
    }))
  }

  const currentCatalog = catalog.projectId === projectId && catalog.session === session ? catalog.value : null
  const currentTasks = tasksState.projectId === projectId && tasksState.session === session ? tasksState : { status: 'loading' as const, tasks: [] }
  const unconfirmed = unconfirmedByProject.get(projectId) ?? null
  return { ...(currentCatalog ?? EMPTY_CATALOG), tasks: currentTasks.tasks, taskStatus: currentTasks.status,
    retryTasks: () => setTaskRevision((value) => value + 1), loaded: currentCatalog !== null,
    loading: Boolean(projectId) && (catalog.projectId !== projectId || catalog.session !== session || loading), loadError,
    busy, error, setError, perform, recordSecret, refresh, unconfirmed,
    canAcknowledge: reviewed && !loading && !loadError, acknowledge,
    stopWaiting: () => interruptRequest.current?.(true) }
}
