import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiProblemError } from '../../src/api'
import { useUserQuery } from '../../src/hooks/useUserRequest'

/** DOM を使わず commit/passive の間を制御する Hook phase 替身。実 React は browser 回帰で別途検証する。 */
interface HookSlot {
  kind: 'state' | 'ref' | 'callback' | 'effect'
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
  vi.stubGlobal('window', { setTimeout, clearTimeout })
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
