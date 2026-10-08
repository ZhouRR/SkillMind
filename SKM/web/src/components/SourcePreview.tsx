import { useEffect, useMemo, useRef, useState } from 'react'
import { useMessages } from '../i18n'
import { previewTextExceedsLimit } from '../lib/previewLimits'
import { markdownSourcePage, markdownSourcePageCount } from '../lib/markdownPreviewPages'
import { PreviewPagination } from './PreviewPagination'

/** 原文・plain text は byte 入場制限と表示頁を分け、全件表示と誤認させない。 */
export function SourcePreview({ source, className = 'previewText' }: { source: string; className?: string }) {
  const messages = useMessages()
  const [selection, setSelection] = useState({ source, index: 0 })
  const readingRef = useRef<HTMLPreElement>(null)
  const count = markdownSourcePageCount(source)
  const index = selection.source === source ? Math.min(selection.index, count - 1) : 0
  const oversized = useMemo(() => previewTextExceedsLimit(source), [source])
  useEffect(() => {
    if (readingRef.current) { readingRef.current.scrollTop = 0; readingRef.current.scrollLeft = 0 }
  }, [source, index])
  if (oversized) return <p className="error" role="alert">{messages.documentsPanel.failures.previewTooLarge}</p>
  const page = markdownSourcePage(source, index)
  return <>
    {count > 1 && <p className="hint">{messages.documentsPanel.sourcePageHint}</p>}
    <PreviewPagination index={index} count={count} select={(next) => setSelection({ source, index: next })} />
    <pre ref={readingRef} className={className} tabIndex={0} role="region" aria-label={messages.assetsAudit.sourceRegion}>{page.source}</pre>
  </>
}
