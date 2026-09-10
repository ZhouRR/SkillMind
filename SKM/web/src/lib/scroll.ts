/** Streaming 追随スクロールの共通判定。画面非依存の純ロジックとして一箇所に固定する。 */

/** 要素が末尾付近(既定 40px 以内)にスクロールされているかを返す。 */
export function isNearBottom(
  element: { scrollHeight: number; scrollTop: number; clientHeight: number },
  threshold = 40,
): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold
}
