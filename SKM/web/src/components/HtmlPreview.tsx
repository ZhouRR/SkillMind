import { useMemo } from 'react'
import { documentPreviewHtml } from '../lib/documentPreview'
import { previewTextExceedsLimit } from '../lib/previewLimits'
import { useMessages } from '../i18n'
import { SourcePreview } from './SourcePreview'
import { StaticPreviewFrame } from './StaticPreviewFrame'

/** 20 MB 入場制限と別に、主 thread の DOM 構築を従来の最大入力以下に保つ。 */
const HTML_RENDER_MAX_CHARACTERS = 1_000_000

/** HTML は任意の位置で切ると構造が変わるため、大きい文書を明示的な原文頁にする。 */
export function HtmlPreview({ source, title, className = 'previewFrame' }: {
  source: string; title: string; className?: string
}) {
  const messages = useMessages()
  const oversized = useMemo(() => previewTextExceedsLimit(source), [source])
  const sourceOnly = source.length > HTML_RENDER_MAX_CHARACTERS
  const html = useMemo(() => oversized || sourceOnly ? '' : documentPreviewHtml(source), [source, oversized, sourceOnly])
  if (oversized) return <p className="error" role="alert">{messages.documentsPanel.failures.previewTooLarge}</p>
  if (sourceOnly) return <>
    <p className="hint">{messages.documentsPanel.htmlSourcePages}</p>
    <SourcePreview source={source} />
  </>
  return <StaticPreviewFrame source={html} title={title} className={className} />
}
