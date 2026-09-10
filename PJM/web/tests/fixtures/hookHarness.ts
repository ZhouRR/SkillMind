/** DOM の描画と独立に layout/passive/microtask の境界を制御する test 専用 runtime。 */
interface Slot { value?: unknown; initialized?: boolean; deps?: readonly unknown[]; cleanup?: void | (() => void) }
export const hookPhases = { cursor: 0, slots: [] as Slot[], layout: [] as Array<() => void>, passive: [] as Array<() => void> }
/** 一つの mount 中に限り state と ref の位置を固定する。 */
function slot(): Slot { return hookPhases.slots[hookPhases.cursor++] ?? (hookPhases.slots[hookPhases.cursor - 1] = {}) }
/** React と同じ参照等価の依存関係だけを再利用する。 */
function same(a?: readonly unknown[], b?: readonly unknown[]): boolean { return Boolean(a && b && a.length === b.length && a.every((item, index) => Object.is(item, b[index]))) }
/** commit を明示するまで旧 effect を自動 cleanup しない。 */
function effect(queue: Array<() => void>, setup: () => void | (() => void), deps?: readonly unknown[]): void {
  const current = slot()
  if (!same(current.deps, deps)) queue.push(() => { current.cleanup?.(); current.deps = deps; current.cleanup = setup() })
}
/** Hook が保持した元 closure を deps が変わるまで維持する。 */
function memo(factory: () => unknown, deps?: readonly unknown[]): unknown {
  const current = slot()
  if (!current.initialized || !same(current.deps, deps)) { current.value = factory(); current.deps = deps; current.initialized = true }
  return current.value
}
export const hookReact = {
  useState: (initial: unknown) => {
    const current = slot()
    if (!current.initialized) { current.value = typeof initial === 'function' ? initial() : initial; current.initialized = true }
    return [current.value, (value: unknown) => { current.value = typeof value === 'function' ? value(current.value) : value }]
  },
  useRef: (initial: unknown) => { const current = slot(); return current.value ?? (current.value = { current: initial }) },
  useCallback: (callback: unknown, deps?: readonly unknown[]) => memo(() => callback, deps), useMemo: memo,
  useEffect: (setup: () => void | (() => void), deps?: readonly unknown[]) => effect(hookPhases.passive, setup, deps),
  useLayoutEffect: (setup: () => void | (() => void), deps?: readonly unknown[]) => effect(hookPhases.layout, setup, deps),
}
/** 同じ tick の操作では state を勝手に再描画しない。 */
export function commitHooks(): void {
  for (const callback of hookPhases.layout.splice(0)) callback()
  for (const callback of hookPhases.passive.splice(0)) callback()
}
/** 破棄済みの owner が新 mount に同じ ref を持ち込まない。 */
export function unmountHooks(): void {
  for (const current of hookPhases.slots) current.cleanup?.()
  hookPhases.cursor = 0; hookPhases.slots = []; hookPhases.layout = []; hookPhases.passive = []
}
/** timer を進めず、transport が返す Promise の連鎖だけを完了させる。 */
export async function hookMicrotasks(): Promise<void> { for (let index = 0; index < 12; index++) await Promise.resolve() }
/** Abort を無視する正常/異常 transport を呼出側で解放する。 */
export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
