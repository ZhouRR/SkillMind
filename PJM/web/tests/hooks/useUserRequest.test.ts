import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiProblemError } from '../../src/api'
import { useUserQuery } from '../../src/hooks/useUserRequest'
import { useResourceMutation, useResourceQuery } from '../../src/hooks/useResourceRequest'
import { PROJECT_MEMBER_REQUEST_POLICY } from '../../src/lib/projectMemberFeedback'

/** DOM を使わず commit/passive の間を制御する Hook phase 替身。実 React は browser 回帰で別途検証する。 */
interface HookSlot {
  kind: 'state' | 'ref' | 'callback' | 'memo' | 'effect'
  value?: unknown
  dependencies?: readonly unknown[]
  cleanup?: void | (() => void)
}

const phases = vi.hoisted(() => ({
  cursor: 0,
  slots: [] as HookSlot[],
  layout: [] as Array<() => void>,
  passive: [] as Array<() => void>,
}))

vi.mock('react', () => {
  /** 呼出順を固定し、render ごとに ref/state を新造して境界の欠陥を隠さない。 */
  const slot = (kind: HookSlot['kind']): HookSlot => {
    const index = phases.cursor++
    const found = phases.slots[index]
    if (found && found.kind !== kind) throw new Error('Unexpected hook order')
    return found ?? (phases.slots[index] = { kind })
  }
  /** React と同じ参照比較で依存の変更を判定する。 */
  const unchanged = (left: readonly unknown[] | undefined, right: readonly unknown[] | undefined): boolean =>
    left !== undefined && right !== undefined && left.length === right.length
    && left.every((value, index) => Object.is(value, right[index]))
  /** cleanup と setup は render 中でなく、テストが選ぶ commit phase で実行する。 */
  const effect = (queue: Array<() => void>, setup: () => void | (() => void), dependencies?: readonly unknown[]): void => {
    const current = slot('effect')
    if (unchanged(current.dependencies, dependencies)) return
    queue.push(() => {
      current.cleanup?.()
      current.dependencies = dependencies
      current.cleanup = setup()
    })
  }
  return {
    useState: (initial: unknown) => {
      const current = slot('state')
      if (!Object.hasOwn(current, 'value')) current.value = initial
      return [current.value, (next: unknown) => {
        current.value = typeof next === 'function' ? next(current.value) : next
      }]
    },
    useRef: (initial: unknown) => {
      const current = slot('ref')
      return current.value ?? (current.value = { current: initial })
    },
    useCallback: (callback: unknown, dependencies: readonly unknown[]) => {
      const current = slot('callback')
      if (!unchanged(current.dependencies, dependencies)) {
        current.value = callback
        current.dependencies = dependencies
      }
      return current.value
    },
    useMemo: (factory: () => unknown, dependencies: readonly unknown[]) => {
      const current = slot('memo')
      if (!unchanged(current.dependencies, dependencies)) {
        current.value = factory()
        current.dependencies = dependencies
      }
      return current.value
    },
    useEffect: (setup: () => void | (() => void), dependencies?: readonly unknown[]) => effect(phases.passive, setup, dependencies),
    useLayoutEffect: (setup: () => void | (() => void), dependencies?: readonly unknown[]) => effect(phases.layout, setup, dependencies),
  }
})

/** 手動 resolve/reject で Abort を無視する transport を再現する。 */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

/** Promise chain だけを進め、タイマーや passive cleanup は勝手に実行しない。 */
async function microtasks(): Promise<void> {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
}

/** 一つの phase に予約された commit を順番どおり実行する。 */
function commit(queue: Array<() => void>): void {
  for (const action of queue.splice(0)) action()
}

/** 本番 hook を同じインスタンスとして再描画する。 */
function render(key: string, loader: (signal: AbortSignal) => Promise<string>, ended: () => void) {
  phases.cursor = 0
  return useUserQuery(key, loader, ended)
}

beforeEach(() => {
  phases.cursor = 0
  phases.slots = []
  phases.layout = []
  phases.passive = []
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval })
})

describe('shared resource request boundaries', () => {
  /** アカウントと別の失敗分類でも同じ非同期境界を利用する。 */
  function resource(key: string, loader: (signal: AbortSignal) => Promise<string>, ended: () => void, enabled = true) {
    phases.cursor = 0
    return useResourceQuery(key, loader, ended, PROJECT_MEMBER_REQUEST_POLICY, enabled)
  }
  /** 一つの mutation instance を再描画し、同期 ref の効果を観察する。 */
  function mutation(ended = vi.fn()) {
    phases.cursor = 0
    return useResourceMutation(ended, PROJECT_MEMBER_REQUEST_POLICY)
  }

  it('does not reuse A facts when returning through a pending B query', async () => {
    const pending = deferred<string>()
    const loader = vi.fn<(signal: AbortSignal) => Promise<string>>()
      .mockResolvedValueOnce('A old facts').mockImplementationOnce(() => pending.promise)
      .mockResolvedValueOnce('A new facts')
    const ended = vi.fn()
    resource('A', loader, ended)
    commit(phases.layout); commit(phases.passive); await microtasks()
    expect(resource('A', loader, ended).data).toBe('A old facts')
    resource('B', loader, ended)
    commit(phases.layout); commit(phases.passive); await microtasks()
    expect(resource('A', loader, ended)).toMatchObject({ data: null, pending: true })
    commit(phases.layout); commit(phases.passive); await microtasks()
    pending.reject(new ApiProblemError('private', 401))
    await microtasks()
    expect(resource('A', loader, ended).data).toBe('A new facts')
    expect(ended).not.toHaveBeenCalled()
  })

  it('pauses a read without accepting its late result and revalidates on resumption', async () => {
    const pending = deferred<string>()
    const loader = vi.fn<(signal: AbortSignal) => Promise<string>>()
      .mockImplementationOnce(() => pending.promise).mockResolvedValueOnce('current')
    const ended = vi.fn()
    resource('A', loader, ended)
    commit(phases.layout); commit(phases.passive); await microtasks()
    expect(resource('A', loader, ended, false).pending).toBe(true)
    commit(phases.layout); commit(phases.passive)
    expect(loader.mock.calls[0]![0].aborted).toBe(true)
    pending.reject(new ApiProblemError('private', 401))
    await microtasks()
    expect(loader).toHaveBeenCalledTimes(1)
    expect(ended).not.toHaveBeenCalled()
    resource('A', loader, ended)
    commit(phases.layout); commit(phases.passive); await microtasks()
    expect(resource('A', loader, ended)).toMatchObject({ data: 'current', pending: false })
  })

  it('retains same-context facts during refresh but does not mark them as current evidence', async () => {
    const pending = deferred<string>()
    const loader = vi.fn<(signal: AbortSignal) => Promise<string>>()
      .mockResolvedValueOnce('prior').mockImplementationOnce(() => pending.promise)
    const ended = vi.fn()
    resource('A', loader, ended)
    commit(phases.layout); commit(phases.passive); await microtasks()
    resource('A', loader, ended).refresh()
    expect(resource('A', loader, ended)).toMatchObject({ data: 'prior', pending: true, completed: -1 })
    commit(phases.layout); commit(phases.passive); await microtasks()
    pending.resolve('current'); await microtasks()
    expect(resource('A', loader, ended)).toMatchObject({ data: 'current', completed: 1, pending: false })
  })

  it('blocks same-tick double writes and requires explicit acknowledgement after interruption', async () => {
    const pending = deferred<string>()
    const operation = vi.fn<(signal: AbortSignal) => Promise<string>>(() => pending.promise)
    const success = vi.fn()
    const failure = vi.fn()
    const write = mutation()
    commit(phases.layout); commit(phases.passive)
    expect(write.submit(operation, success, failure)).toBe(true)
    expect(write.submit(operation, success)).toBe(false)
    await microtasks()
    write.interrupt()
    expect(operation.mock.calls[0]![0].aborted).toBe(true)
    expect(failure).toHaveBeenCalledExactlyOnceWith({ key: 'unknown' })
    expect(write.submit(operation, success)).toBe(false)
    pending.resolve('late accepted'); await microtasks()
    expect(success).not.toHaveBeenCalled()
    expect(mutation()).toMatchObject({ busy: false, failure: { key: 'unknown' } })
    write.acknowledge()
    expect(write.submit(async () => 'new explicit write', success)).toBe(true)
    await microtasks()
    expect(success).toHaveBeenCalledExactlyOnceWith('new explicit write')
    expect(operation).toHaveBeenCalledTimes(1)
  })

  it('closes a timed-out write as unknown even if the delayed server result succeeds', async () => {
    const pending = deferred<string>()
    const success = vi.fn()
    const write = mutation()
    commit(phases.layout); commit(phases.passive)
    write.submit(() => pending.promise, success)
    await microtasks()
    vi.advanceTimersByTime(30_000)
    pending.resolve('late'); await microtasks()
    expect(mutation()).toMatchObject({ busy: false, failure: { key: 'unknown' } })
    expect(success).not.toHaveBeenCalled()
    expect(write.submit(async () => 'replay', success)).toBe(false)
  })

  it('does not start a queued mutation after layout unmount', async () => {
    const operation = vi.fn(async () => 'never sent')
    const ended = vi.fn()
    const write = mutation(ended)
    commit(phases.layout); commit(phases.passive)
    write.submit(operation, vi.fn())
    for (const current of phases.slots) current.cleanup?.()
    await microtasks()
    expect(operation).not.toHaveBeenCalled()
    expect(ended).not.toHaveBeenCalled()
  })
})

afterEach(() => {
  for (const current of phases.slots) current.cleanup?.()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('account query commit boundaries', () => {
  it('invalidates an old 401 synchronously when the same-key refresh is requested', async () => {
    const old = deferred<string>()
    const next = deferred<string>()
    const loader = vi.fn<(signal: AbortSignal) => Promise<string>>()
      .mockImplementationOnce(() => old.promise)
      .mockImplementationOnce(() => next.promise)
    const ended = vi.fn()
    const query = render('same-account', loader, ended)
    commit(phases.layout); commit(phases.passive)
    await microtasks()
    const oldSignal = loader.mock.calls[0]![0]

    query.refresh()
    expect(oldSignal.aborted).toBe(true)
    old.reject(new ApiProblemError('not displayed', 401))
    await microtasks()
    expect(ended).not.toHaveBeenCalled()

    expect(render('same-account', loader, ended).pending).toBe(true)
    commit(phases.layout); commit(phases.passive)
    await microtasks()
    next.resolve('new facts')
    await microtasks()
    expect(render('same-account', loader, ended).data).toBe('new facts')
    expect(loader).toHaveBeenCalledTimes(2)
  })

  it('rejects a replaced same-key loader response between layout and passive cleanup', async () => {
    const old = deferred<string>()
    const first = vi.fn<(signal: AbortSignal) => Promise<string>>(() => old.promise)
    const next = vi.fn<(signal: AbortSignal) => Promise<string>>().mockResolvedValue('current facts')
    const ended = vi.fn()
    render('same-account', first, ended)
    commit(phases.layout); commit(phases.passive)
    await microtasks()

    render('same-account', next, ended)
    commit(phases.layout)
    expect(first.mock.calls[0]![0].aborted).toBe(true)
    old.reject(new ApiProblemError('not displayed', 401))
    await microtasks()
    expect(ended).not.toHaveBeenCalled()

    commit(phases.passive)
    await microtasks()
    expect(render('same-account', next, ended).data).toBe('current facts')
  })

  it('aborts a read in layout unmount before the passive cleanup can run', async () => {
    const old = deferred<string>()
    const loader = vi.fn<(signal: AbortSignal) => Promise<string>>(() => old.promise)
    const ended = vi.fn()
    render('account', loader, ended)
    commit(phases.layout)
    const layoutCleanup = phases.slots.find((current) => current.kind === 'effect')!.cleanup
    commit(phases.passive)
    await microtasks()

    layoutCleanup?.()
    expect(loader.mock.calls[0]![0].aborted).toBe(true)
    old.reject(new ApiProblemError('not displayed', 401))
    await microtasks()
    expect(ended).not.toHaveBeenCalled()
  })
})
