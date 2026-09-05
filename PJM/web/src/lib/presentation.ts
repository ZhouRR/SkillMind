/** 画面横断で共有する表示用の純 logic。Run 見出しと timestamp の変換をここへ一元化する。 */

/** Run history・概览の主見出しを業務 input field に依存せず決める。


    要約が無い場合の代替見出しは利用者言語に依存するため、文言の組み立ては
    catalog 側(`elements.runFallbackTitle`)を呼び出し元から明示的に受け取る。 */
export function runHistoryTitle(
  resultSummary: string | null,
  runId: string,
  fallbackTitle: (shortId: string) => string,
): string {
  if (resultSummary) return resultSummary
  return fallbackTitle(runId.slice(0, 8))
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
