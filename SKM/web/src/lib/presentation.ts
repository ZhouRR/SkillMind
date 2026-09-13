/** 画面横断で共有する表示用の純 logic。Run 見出しと timestamp の変換をここへ一元化する。 */

/** 不変 task 名を主見出しにし、古い API/履歴は要約または言語別の代替名へ戻す。 */
export function runHistoryTitle(
  item: { task_title?: string | null; result_summary: string | null },
  fallbackTitle: string,
): string {
  return item.task_title?.trim() || item.result_summary?.trim() || fallbackTitle
}

/** ISO timestamp を browser locale の日時へ変換する。不正値は原文のまま返す。 */
export function formatLocalTimestamp(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString()
}

/** ISO timestamp を browser locale の時刻だけへ変換する（同日 timeline 等の高密度表示用）。 */
export function formatLocalTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleTimeString()
}

/** Byte 数を人が読める 1 単位の概数へ変換する（文書一覧と Skill 上传 preview が共用）。 */
export function formatByteSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
