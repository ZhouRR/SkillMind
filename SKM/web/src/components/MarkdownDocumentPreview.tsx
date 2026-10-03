import { MarkdownPreview } from './MarkdownPreview'

/** 文書管理は共有 preview の sandbox 表示と原文操作を使用する。 */
export function MarkdownDocumentPreview({ source, title }: { source: string; title: string }) {
  return <MarkdownPreview source={source} title={title} />
}
