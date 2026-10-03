import { useEffect, useMemo, useState } from 'react'
import { useMessages } from '../i18n'
import { documentMarkdownPageHtml } from '../lib/documentPreview'
import { readingMarkdownPage } from '../lib/markdown'
import { useMermaidPreviews } from '../hooks/useMermaidPreviews'
import { useMarkdownPreview } from '../hooks/useMarkdownPreview'
import { PreviewPagination } from './PreviewPagination'
import { SourcePreview } from './SourcePreview'

/** sandbox 文書へ親の配色 token だけ渡す。文書原文や外部 CSS は混ぜない。 */
function previewThemeCss(): string {
  if (typeof document === 'undefined') return ''
  const style = getComputedStyle(document.documentElement)
  return `:root{color-scheme:${style.colorScheme};color:${style.getPropertyValue('--text')};background:${style.getPropertyValue('--surface')};--border:${style.getPropertyValue('--border')};--raised:${style.getPropertyValue('--surface-raised')}}`
}

/** 文書・抜粋・報告の頁管理を共有し、表示 context 固有の安全 renderer だけ切り替える。 */
export function MarkdownPreview({ source, title = '', mode = 'document' }: {
  source: string; title?: string; mode?: 'document' | 'reading'
}) {
  const messages = useMessages()
  const labels = messages.documentsPanel
  const [sourceSelection, setSourceSelection] = useState<string | null>(null)
  const showSource = sourceSelection === source
  const preview = useMarkdownPreview(source, !showSource, mode)
  const [selection, setSelection] = useState({ source, index: 0 })
  const wantedIndex = selection.source === source ? selection.index : 0
  useEffect(() => {
    if (!showSource && preview.count > 0 && (preview.status === 'ready' || preview.status === 'error')) {
      const index = Math.min(wantedIndex, preview.count - 1)
      if (preview.index !== index) preview.select(index)
    }
  }, [showSource, wantedIndex, preview.count, preview.status, preview.index, preview.select])
  const select = (index: number): void => { setSelection({ source, index }); preview.select(index) }
  const [themeCss, setThemeCss] = useState(() => mode === 'document' ? previewThemeCss() : '')
  useEffect(() => {
    if (mode !== 'document') return
    const update = (): void => setThemeCss(previewThemeCss())
    const observer = new MutationObserver(update)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    update()
    return () => observer.disconnect()
  }, [mode])
  const page = preview.page
  const diagrams = useMermaidPreviews(page?.tokens, !showSource && !page?.sourceOnly)
  const diagramLabels = useMemo(() => ({ loading: labels.mermaidLoading, failed: labels.mermaidFailed, title: labels.mermaidTitle }), [labels])
  const html = useMemo(() => showSource || !page || page.sourceOnly ? ''
    : mode === 'reading' ? readingMarkdownPage(page.tokens, page.source, { previews: diagrams, labels: diagramLabels })
      : documentMarkdownPageHtml(page.tokens, page.source, { previews: diagrams, labels: diagramLabels }), [page, showSource, mode, diagrams, diagramLabels])
  const themedHtml = useMemo(() => html.replace('</head>', `<style>${themeCss}</style></head>`), [html, themeCss])
  return <>
    {(mode === 'document' || preview.count > 1) && <div className="markdownPreviewToolbar">
      {mode === 'document' && <button type="button" className="secondaryButton compactButton" aria-pressed={showSource}
        onClick={() => setSourceSelection(showSource ? null : source)}>
        {showSource ? labels.previewButton : labels.viewSource}
      </button>}
      {!showSource && <PreviewPagination index={preview.index} count={preview.count} select={select} />}
    </div>}
    {showSource ? <SourcePreview source={source} />
      : preview.status === 'loading' ? <p role="status">{labels.loadingPreview}</p>
        : preview.failure === 'tooLarge' ? <p className="error" role="alert">{labels.failures.previewTooLarge}</p>
          : <>
            {preview.failure && <p className="hint" role="status">{labels.markdownPreviewFailed}</p>}
            {!preview.failure && page?.sourceOnly && <p className="hint">{labels.markdownSourcePage}</p>}
            {page?.sourceOnly ? mode === 'reading' ? <div className="readingMarkdown"><pre>{page.source}</pre></div>
              : <pre className="previewText">{page.source}</pre>
              : mode === 'reading' ? <div className="readingMarkdown" dangerouslySetInnerHTML={{ __html: html }} />
                : <iframe key={preview.index} className="previewFrame" sandbox="" referrerPolicy="no-referrer" srcDoc={themedHtml} title={title} />}
          </>}
  </>
}
