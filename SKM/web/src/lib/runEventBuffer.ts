/** 不変配列の append 系譜。配列そのものを value に保持せず、過去の配列を解放可能にする。 */
interface AppendLineage { length: number; parent?: AppendLineage }
const appendLineages = new WeakMap<object, AppendLineage>()

/** 共通 helper の不変 prefix を追加 batch 数で判定する。未知の配列は呼出側で検証する。 */
export function hasAppendOnlyPrefix<T>(previous: T[], current: T[]): boolean {
  const lineage = appendLineages.get(previous)
  let candidate = appendLineages.get(current)
  if (lineage === undefined || candidate === undefined) return false
  while (candidate.length > lineage.length && candidate.parent !== undefined) candidate = candidate.parent
  return candidate === lineage
}

/** 既存 API 用の単一 event 追加。重複は first-wins とし、元配列は変更しない。 */
export function appendOrderedEvent<T extends { sequence: number }>(current: T[], incoming: T): T[] {
  return appendOrderedEvents(current, [incoming])
}

/** 一つの burst を安定 sort/merge し、通常の末尾追加では履歴を一度だけコピーする。 */
export function appendOrderedEvents<T extends { sequence: number }>(current: T[], incoming: readonly T[]): T[] {
  if (incoming.length === 0) return current
  const ordered = [...incoming].sort((left, right) => left.sequence - right.sequence)
  const additions = ordered.filter((event, index) => index === 0 || event.sequence !== ordered[index - 1]!.sequence)
  const last = current.at(-1)
  if (last === undefined || additions[0]!.sequence > last.sequence) {
    const lineage = appendLineages.get(current) ?? { length: current.length }
    appendLineages.set(current, lineage)
    const result = [...current, ...additions]
    appendLineages.set(result, { length: result.length, parent: lineage })
    return result
  }
  const result: T[] = []
  let index = 0
  let added = false
  for (const event of additions) {
    while (index < current.length && current[index]!.sequence < event.sequence) result.push(current[index++]!)
    if (current[index]?.sequence !== event.sequence) { result.push(event); added = true }
  }
  if (!added) return current
  while (index < current.length) result.push(current[index++]!)
  return result
}

/** 一時 delta を監査から除外し、append burst では新しい範囲だけを見る。 */
export function createAuditEventProjector<T extends { event_type: string }>(): (events: T[]) => T[] {
  let previous: T[] = []
  let audit: T[] = []
  return (events) => {
    if (previous === events) return audit
    if (hasAppendOnlyPrefix(previous, events)) {
      const additions = events.slice(previous.length).filter((event) => event.event_type !== 'TEXT_DELTA')
      if (additions.length > 0) audit = [...audit, ...additions]
    } else audit = events.filter((event) => event.event_type !== 'TEXT_DELTA')
    previous = events
    return audit
  }
}

/** 短い timer window と件数上限で burst をまとめる。背景 tab の rAF 停止には依存しない。 */
export function createEventBatcher<T>(commit: (events: T[]) => void, delay = 16, limit = 256) {
  let pending: T[] = []
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  /** 完了・接続切替より前に保留 event を同期的に引き渡す。 */
  function flush(): void {
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
    if (pending.length === 0) return
    const events = pending
    pending = []
    commit(events)
  }
  return {
    push(event: T): void {
      if (disposed) return
      pending.push(event)
      if (pending.length >= limit) flush()
      else if (timer === undefined) timer = setTimeout(flush, delay)
    },
    flush,
    /** 終了した所有者の遅延 callback を破棄し、新 Run に混ぜない。 */
    dispose(): void {
      disposed = true
      if (timer !== undefined) clearTimeout(timer)
      timer = undefined
      pending = []
    },
  }
}
