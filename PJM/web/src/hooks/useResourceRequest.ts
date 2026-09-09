import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

/** 資源固有の文案を持たず、安定した分類と待機時間だけを伝える。 */
export interface ResourceFailure {
  key: string
  retryAfterSeconds?: number
}

/** HTTP の既知拒否と未知結果の意味は各資源が定義する。 */
export interface ResourceRequestPolicy<F extends ResourceFailure> {
  classify: (error: unknown, mutation: boolean) => F
  readTimeout: F
  writeTimeout: F
  blocks: (failure: F) => boolean
}

/** HTTP 待機の上限。期限超過は Server の rollback を意味しない。 */
export const RESOURCE_REQUEST_TIMEOUT_MS = 30_000

/** 失効通知は現在の会話に限定し、成功した自己失効と 401 を区別する。 */
export type SessionEnded = (reason?: 'expired' | 'revoked') => void

/** 現在 query に対応した response。更新中も元データを保ち、草稿を消さない。 */
interface QueryState<T, F extends ResourceFailure> {
  request: { context: { key: string }; loader: (signal: AbortSignal) => Promise<T>; revision: number }
  completed: number
  data: T | null
  failure: F | null
}

/** query 切替・refresh・unmount より古い読取結果を表示しない。 */
export function useResourceQuery<T, F extends ResourceFailure>(
  key: string, loader: (signal: AbortSignal) => Promise<T>, onSessionEnded: SessionEnded,
  policy: ResourceRequestPolicy<F>, enabled = true,
) {
  const [revision, setRevision] = useState(0)
  const context = useMemo(() => ({ key }), [key])
  const request = useMemo(() => ({ context, loader, revision, policy, enabled }), [context, loader, revision, policy, enabled])
  const [state, setState] = useState<QueryState<T, F>>({ request, completed: -1, data: null, failure: null })
  const currentRequest = useRef(request)
  const active = useRef<AbortController | null>(null)
  currentRequest.current = request
  const ended = useRef(onSessionEnded)
  ended.current = onSessionEnded
  /** 手動再読取を要求した瞬間に旧世代を閉じ、再描画前の古い 401 も捨てる。 */
  const refresh = useCallback(() => {
    active.current?.abort()
    active.current = null
    setRevision((value) => value + 1)
  }, [])

  // 対象/読取世代の変更と同じ commit で閉じ、passive cleanup 前の microtask も通さない。
  useLayoutEffect(() => () => {
    active.current?.abort()
    active.current = null
  }, [request, context, loader, revision, policy])

  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    const sessionEnded = ended.current
    active.current = controller
    let settled = false
    const expiresAt = performance.now() + RESOURCE_REQUEST_TIMEOUT_MS
    setState((old) => ({ request, completed: -1, data: old.request.context === context ? old.data : null, failure: null }))
    /** Abort を無視する transport から来た結果も request 世代で破棄する。 */
    const current = (): boolean => !settled && !controller.signal.aborted
      && active.current === controller && currentRequest.current === request
    /** Background tab で timer が遅れても、処理済みという成功には変えない。 */
    const expire = (): void => {
      if (!current()) return
      settled = true
      window.clearTimeout(timer)
      controller.abort()
      if (active.current === controller) active.current = null
      setState((old) => ({ ...old, completed: revision, failure: policy.readTimeout }))
    }
    const timer = window.setTimeout(expire, RESOURCE_REQUEST_TIMEOUT_MS)
    void Promise.resolve().then(() => {
      if (!current()) controller.abort()
      if (performance.now() >= expiresAt) expire()
      controller.signal.throwIfAborted()
      return loader(controller.signal)
    }).then((data) => {
      if (!current()) return
      if (performance.now() >= expiresAt) { expire(); return }
      settled = true
      window.clearTimeout(timer)
      active.current = null
      setState({ request, completed: revision, data, failure: null })
    }).catch((error: unknown) => {
      if (!current()) return
      if (performance.now() >= expiresAt) { expire(); return }
      settled = true
      window.clearTimeout(timer)
      active.current = null
      const failure = policy.classify(error, false)
      // 並行した事実読取の片方が失敗した場合も、残った transport を閉じる。
      controller.abort()
      setState((old) => ({ ...old, completed: revision, failure }))
      if (failure.key === 'sessionExpired') sessionEnded()
    })
    return () => {
      settled = true
      window.clearTimeout(timer)
      controller.abort()
      if (active.current === controller) active.current = null
    }
  }, [request, context, loader, revision, policy, enabled])

  return {
    data: enabled && state.request.context === context ? state.data : null,
    failure: state.request === request ? state.failure : null,
    pending: !enabled || state.request !== request || state.completed !== revision,
    completed: state.request === request ? state.completed : -1,
    revision,
    refresh,
  }
}

/** 画面内の一つの書込。state の再描画を待たずに二重 submit を拒否する。 */
export function useResourceMutation<F extends ResourceFailure>(
  onSessionEnded: SessionEnded, policy: ResourceRequestPolicy<F>,
) {
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<F | null>(null)
  const [cooldown, setCooldown] = useState(0)
  const active = useRef<AbortController | null>(null)
  const mounted = useRef(false)
  const blocked = useRef(false)
  const currentPolicy = useRef(policy)
  currentPolicy.current = policy
  const deadline = useRef(0)
  const timeout = useRef<number | undefined>(undefined)
  const interruptRequest = useRef<(() => void) | null>(null)
  const ended = useRef(onSessionEnded)
  ended.current = onSessionEnded

  // Request の受付/破棄は commit に合わせ、離頁後の書込開始や失効通知を防ぐ。
  useLayoutEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      window.clearTimeout(timeout.current)
      active.current?.abort()
      active.current = null
      interruptRequest.current = null
    }
  }, [])

  useEffect(() => {
    if (!cooldown) return
    const timer = window.setInterval(() => {
      setCooldown(Math.max(0, Math.ceil((deadline.current - performance.now()) / 1000)))
    }, 250)
    return () => window.clearInterval(timer)
  }, [cooldown > 0])

  /** 最新事実を人が確認した後だけ unknown/conflict の送信禁止を解除する。 */
  const acknowledge = useCallback(() => {
    if (active.current) return
    blocked.current = false
    // 事実の再確認は別の既知拒否や有効な cooldown を解除した証明にはならない。
    setFailure((current) => current && currentPolicy.current.blocks(current) ? null : current)
  }, [])

  /** 同じ対象の資格再確認中など、UI が中断しても Server rollback は推測しない。 */
  const interrupt = useCallback(() => interruptRequest.current?.(), [])

  /** Callback 自体も現在 request の中で呼び、離頁後の外部 state 更新を防ぐ。 */
  const submit = useCallback(<T,>(
    operation: (signal: AbortSignal) => Promise<T>,
    onSuccess: (value: T) => void,
    onFailure?: (reason: F) => void,
  ): boolean => {
    if (!mounted.current || active.current || blocked.current || performance.now() < deadline.current) return false
    const controller = new AbortController()
    const activePolicy = currentPolicy.current
    const sessionEnded = ended.current
    const expiresAt = performance.now() + RESOURCE_REQUEST_TIMEOUT_MS
    active.current = controller
    setBusy(true)
    setFailure(null)
    /** 同じ request のみが結果・失効を通知できる。 */
    const current = (): boolean => mounted.current && active.current === controller && !controller.signal.aborted
    /** 既知拒否と未知を保持し、後続動作へ機密値や request 関数を保存しない。 */
    const reject = (reason: F): void => {
      if (!current()) return
      window.clearTimeout(timeout.current)
      active.current = null
      interruptRequest.current = null
      controller.abort()
      blocked.current = activePolicy.blocks(reason)
      const seconds = reason.key === 'rateLimited' ? reason.retryAfterSeconds ?? 0 : 0
      // 巨大な Retry-After を setTimeout へ渡さず、単調時計の残時間として保持する。
      deadline.current = performance.now() + seconds * 1000
      setCooldown(seconds)
      setFailure(reason)
      setBusy(false)
      if (reason.key === 'sessionExpired') sessionEnded()
      else onFailure?.(reason)
    }
    interruptRequest.current = () => reject(activePolicy.writeTimeout)
    timeout.current = window.setTimeout(() => reject(activePolicy.writeTimeout), RESOURCE_REQUEST_TIMEOUT_MS)
    void Promise.resolve().then(() => {
      if (performance.now() >= expiresAt) reject(activePolicy.writeTimeout)
      controller.signal.throwIfAborted()
      return operation(controller.signal)
    }).then((value) => {
      if (!current()) return
      if (performance.now() >= expiresAt) { reject(activePolicy.writeTimeout); return }
      window.clearTimeout(timeout.current)
      active.current = null
      interruptRequest.current = null
      setBusy(false)
      onSuccess(value)
    }).catch((error: unknown) => reject(performance.now() >= expiresAt
      ? activePolicy.writeTimeout : activePolicy.classify(error, true)))
    return true
  }, [])

  return { busy, failure, cooldown, submit, acknowledge, interrupt }
}
