import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

import { userFailure, type UserFailure } from '../lib/userFeedback'

/** HTTP 待機の上限。期限超過は Server の rollback を意味しない。 */
export const USER_REQUEST_TIMEOUT_MS = 30_000

/** 失効通知は現在の会話に限定し、成功した自己失効と 401 を区別する。 */
export type SessionEnded = (reason?: 'expired' | 'revoked') => void

/** 現在 query に対応した response。更新中も元データを保ち、草稿を消さない。 */
interface QueryState<T> {
  key: string
  completed: number
  data: T | null
  failure: UserFailure | null
}

/** query 切替・refresh・unmount より古い読取結果を表示しない。 */
export function useUserQuery<T>(key: string, loader: (signal: AbortSignal) => Promise<T>, onSessionEnded: SessionEnded) {
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState<QueryState<T>>({ key, completed: -1, data: null, failure: null })
  const currentKey = useRef(key)
  const active = useRef<AbortController | null>(null)
  currentKey.current = key
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
  }, [key, loader, revision])

  useEffect(() => {
    const controller = new AbortController()
    active.current = controller
    let settled = false
    const expiresAt = performance.now() + USER_REQUEST_TIMEOUT_MS
    setState((old) => ({ key, completed: -1, data: old.key === key ? old.data : null, failure: null }))
    /** Abort を無視する transport から来た結果も request 世代で破棄する。 */
    const current = (): boolean => !settled && !controller.signal.aborted
      && active.current === controller && currentKey.current === key
    /** Background tab で timer が遅れても、処理済みという成功には変えない。 */
    const expire = (): void => {
      if (!current()) return
      settled = true
      window.clearTimeout(timer)
      controller.abort()
      if (active.current === controller) active.current = null
      setState((old) => ({ ...old, completed: revision, failure: { key: 'loadFailed' } }))
    }
    const timer = window.setTimeout(expire, USER_REQUEST_TIMEOUT_MS)
    void Promise.resolve().then(() => {
      if (performance.now() >= expiresAt) expire()
      controller.signal.throwIfAborted()
      return loader(controller.signal)
    }).then((data) => {
      if (!current()) return
      if (performance.now() >= expiresAt) { expire(); return }
      settled = true
      window.clearTimeout(timer)
      active.current = null
      setState({ key, completed: revision, data, failure: null })
    }).catch((error: unknown) => {
      if (!current()) return
      if (performance.now() >= expiresAt) { expire(); return }
      settled = true
      window.clearTimeout(timer)
      active.current = null
      const failure = userFailure(error, false)
      setState((old) => ({ ...old, completed: revision, failure }))
      if (failure.key === 'sessionExpired') ended.current()
    })
    return () => {
      settled = true
      window.clearTimeout(timer)
      controller.abort()
      if (active.current === controller) active.current = null
    }
  }, [key, loader, revision])

  return {
    data: state.key === key ? state.data : null,
    failure: state.key === key ? state.failure : null,
    pending: state.key !== key || state.completed !== revision,
    completed: state.key === key ? state.completed : -1,
    revision,
    refresh,
  }
}

/** 画面内の一つの書込。state の再描画を待たずに二重 submit を拒否する。 */
export function useUserMutation(onSessionEnded: SessionEnded) {
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<UserFailure | null>(null)
  const [cooldown, setCooldown] = useState(0)
  const active = useRef<AbortController | null>(null)
  const mounted = useRef(false)
  const blocked = useRef(false)
  const deadline = useRef(0)
  const timeout = useRef<number | undefined>(undefined)
  const ended = useRef(onSessionEnded)
  ended.current = onSessionEnded

  // Request の受付/破棄は commit に合わせ、離頁後の password POST や失効通知を防ぐ。
  useLayoutEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      window.clearTimeout(timeout.current)
      active.current?.abort()
      active.current = null
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
    // 新版の採用は password 拒否や有効な cooldown を解除した証明にはならない。
    setFailure((current) => current?.key === 'unknown' || current?.key === 'versionConflict' ? null : current)
  }, [])

  /** Callback 自体も現在 request の中で呼び、離頁後の外部 state 更新を防ぐ。 */
  const submit = useCallback(<T,>(
    operation: (signal: AbortSignal) => Promise<T>,
    onSuccess: (value: T) => void,
    onFailure?: (reason: UserFailure) => void,
  ): boolean => {
    if (!mounted.current || active.current || blocked.current || performance.now() < deadline.current) return false
    const controller = new AbortController()
    const expiresAt = performance.now() + USER_REQUEST_TIMEOUT_MS
    active.current = controller
    setBusy(true)
    setFailure(null)
    /** 同じ request のみが結果・失効を通知できる。 */
    const current = (): boolean => mounted.current && active.current === controller && !controller.signal.aborted
    /** 既知拒否と未知を保持し、後続動作へ password や request 関数を保存しない。 */
    const reject = (reason: UserFailure): void => {
      if (!current()) return
      window.clearTimeout(timeout.current)
      active.current = null
      controller.abort()
      blocked.current = reason.key === 'unknown' || reason.key === 'versionConflict'
      const seconds = reason.key === 'rateLimited' ? reason.retryAfterSeconds ?? 0 : 0
      // 巨大な Retry-After を setTimeout へ渡さず、単調時計の残時間として保持する。
      deadline.current = performance.now() + seconds * 1000
      setCooldown(seconds)
      setFailure(reason)
      setBusy(false)
      if (reason.key === 'sessionExpired') ended.current()
      else onFailure?.(reason)
    }
    timeout.current = window.setTimeout(() => reject({ key: 'unknown' }), USER_REQUEST_TIMEOUT_MS)
    void Promise.resolve().then(() => {
      if (performance.now() >= expiresAt) reject({ key: 'unknown' })
      controller.signal.throwIfAborted()
      return operation(controller.signal)
    }).then((value) => {
      if (!current()) return
      if (performance.now() >= expiresAt) { reject({ key: 'unknown' }); return }
      window.clearTimeout(timeout.current)
      active.current = null
      setBusy(false)
      onSuccess(value)
    }).catch((error: unknown) => reject(performance.now() >= expiresAt
      ? { key: 'unknown' } : userFailure(error, true)))
    return true
  }, [])

  return { busy, failure, cooldown, submit, acknowledge }
}
