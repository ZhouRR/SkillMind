import { useMemo } from 'react'
import { readingMarkdown } from '../lib/markdown'

/** Platform の固定書式で表示し、model に HTML/CSS を生成させない。 */
export function MarkdownText({ text }: { text: string }) {
  const html = useMemo(() => readingMarkdown(text), [text])
  return <div className="readingMarkdown" dangerouslySetInnerHTML={{ __html: html }} />
}
