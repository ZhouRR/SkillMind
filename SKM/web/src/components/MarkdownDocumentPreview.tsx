import { useEffect, useMemo, useState } from 'react'
import { useMessages } from '../i18n'
import { documentMarkdownPageHtml } from '../lib/documentPreview'
import { markdownPreviewPages } from '../lib/markdownPreviewPages'

/** sandbox 文書へ親の配色 token だけ渡す。文書原文や外部 CSS は混ぜない。 */
function previewThemeCss(): string {
  if (typeof document === 'undefined') return ''
  const style = getComputedStyle(document.documentElement)
  return `:root{color-scheme:${style.colorScheme};color:${style.getPropertyValue('--text')};background:${style.getPropertyValue('--surface')};--border:${style.getPropertyValue('--border')};--raised:${style.getPropertyValue('--surface-raised')}}`
}

/** 文書本文は一頁だけ iframe に載せ、全文原文への切替と直接の頁選択を保つ。 */
export function MarkdownDocumentPreview({ source, title }: { source: string; title: string }) {
  const messages = useMessages()
  const labels = messages.documentsPanel
  const pages = useMemo(() => markdownPreviewPages(source), [source])
  const [selection, setSelection] = useState({ source, index: 0 })
  const [showSource, setShowSource] = useState(false)
  const [themeCss, setThemeCss] = useState(previewThemeCss)
  useEffect(() => {
    const update = (): void => setThemeCss(previewThemeCss())
    const observer = new MutationObserver(update)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    update()
    return () => observer.disconnect()
  }, [])
  const index = selection.source === source ? Math.min(selection.index, pages.length - 1) : 0
  const page = pages[index]!
  const html = useMemo(() => showSource ? '' : documentMarkdownPageHtml(page.tokens, page.source), [page, showSource])
  const themedHtml = useMemo(() => html.replace('</head>', `<style>${themeCss}</style></head>`), [html, themeCss])
  return <>
    <div className="markdownPreviewToolbar">
      <button type="button" className="secondaryButton compactButton" aria-pressed={showSource}
        onClick={() => setShowSource((current) => !current)}>
        {showSource ? labels.previewButton : labels.viewSource}
      </button>
      {!showSource && pages.length > 1 && <nav className="markdownPreviewPagination" aria-label={labels.previewPages}>
        <button type="button" className="secondaryButton compactButton" disabled={index === 0}
          onClick={() => setSelection({ source, index: index - 1 })}>{messages.runHistory.previous}</button>
        <select aria-label={labels.previewPages} value={index}
          onChange={(event) => setSelection({ source, index: Number(event.target.value) })}>
          {pages.map((item, ordinal) => <option key={ordinal} value={ordinal}>
            {labels.previewPage(ordinal + 1, pages.length)}{item.heading ? ` · ${item.heading}` : ''}
          </option>)}
        </select>
        <button type="button" className="secondaryButton compactButton" disabled={index === pages.length - 1}
          onClick={() => setSelection({ source, index: index + 1 })}>{messages.runHistory.next}</button>
      </nav>}
    </div>
    {showSource ? <pre className="previewText">{source}</pre>
      : <iframe key={index} className="previewFrame" sandbox="" referrerPolicy="no-referrer" srcDoc={themedHtml} title={title} />}
  </>
}
