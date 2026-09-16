/** 昇順の不変 event 配列へ追加する。通常の末尾追加では全履歴を sort し直さない。 */
export function appendOrderedEvent<T extends { sequence: number }>(current: T[], incoming: T): T[] {
  const last = current.at(-1)
  if (last === undefined || incoming.sequence > last.sequence) return [...current, incoming]
  let low = 0
  let high = current.length
  while (low < high) {
    const middle = low + Math.floor((high - low) / 2)
    if (current[middle]!.sequence < incoming.sequence) low = middle + 1
    else high = middle
  }
  // 永続 event の再送は first-wins とし、同じ sequence の本文を置き換えない。
  if (current[low]?.sequence === incoming.sequence) return current
  return [...current.slice(0, low), incoming, ...current.slice(low)]
}
