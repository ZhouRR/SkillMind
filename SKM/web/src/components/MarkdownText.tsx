import { MarkdownPreview } from './MarkdownPreview'

/** 抜粋・報告も文書管理と同じ頁管理を使い、固定書式の安全な本文を表示する。 */
export function MarkdownText({ text }: { text: string }) {
  return <MarkdownPreview source={text} mode="reading" />
}
