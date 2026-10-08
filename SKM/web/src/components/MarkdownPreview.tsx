import { useEffect, useMemo, useRef, useState } from 'react'
import { useMessages } from '../i18n'
import { documentMarkdownPageHtml } from '../lib/documentPreview'
import { readingMarkdownPage } from '../lib/markdown'
import { useMermaidPreviews } from '../hooks/useMermaidPreviews'
import { useMarkdownPreview } from '../hooks/useMarkdownPreview'
import { usePreviewTheme } from '../hooks/usePreviewTheme'
import { previewThemeCss } from '../lib/previewTheme'
import { PreviewPagination } from './PreviewPagination'
import { SourcePreview } from './SourcePreview'
import { StaticPreviewFrame } from './StaticPreviewFrame'

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
  const theme = usePreviewTheme()
  const page = preview.page
  const readingRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const region = readingRef.current
    if (region) { region.scrollTop = 0; region.scrollLeft = 0; const pre = region.querySelector('pre'); if (pre) { pre.scrollTop = 0; pre.scrollLeft = 0 } }
  }, [source, preview.index, showSource])
  const diagrams = useMermaidPreviews(page?.tokens, !showSource && !page?.sourceOnly, theme)
  const diagramLabels = useMemo(() => ({ loading: labels.mermaidLoading, failed: labels.mermaidFailed, title: labels.mermaidTitle }), [labels])
  const html = useMemo(() => showSource || !page || page.sourceOnly ? ''
    : mode === 'reading' ? readingMarkdownPage(page.tokens, page.source, { previews: diagrams, labels: diagramLabels })
      : documentMarkdownPageHtml(page.tokens, page.source, { previews: diagrams, labels: diagramLabels }), [page, showSource, mode, diagrams, diagramLabels])
  const themedHtml = useMemo(() => html.replace('</head>', `<style>${previewThemeCss(theme)}</style></head>`), [html, theme])
  return <>
    {(mode === 'document' || preview.count > 1) && <div className="markdownPreviewToolbar">
      {mode === 'document' && <button type="button" className="secondaryButton compactButton" aria-pressed={showSource}
        onClick={() => setSourceSelection(showSource ? null : source)}>
        {showSource ? labels.previewButton : labels.viewSource}
      </button>}
      {!showSource && <PreviewPagination index={preview.index} count={preview.count} select={select} />}
    </div>}
    <div className="markdownPreviewPage" ref={readingRef}>
    {showSource ? <SourcePreview source={source} />
      : preview.status === 'loading' ? <p role="status">{labels.loadingPreview}</p>
        : preview.failure === 'tooLarge' ? <p className="error" role="alert">{labels.failures.previewTooLarge}</p>
          : <>
            {preview.failure && <p className="hint" role="status">{labels.markdownPreviewFailed}</p>}
            {!preview.failure && page?.sourceOnly && <p className="hint">{labels.markdownSourcePage}</p>}
            {page?.sourceOnly ? mode === 'reading' ? <div className="readingMarkdown"><pre tabIndex={0} role="region" aria-label={messages.assetsAudit.sourceRegion}>{page.source}</pre></div>
              : <pre className="previewText" tabIndex={0} role="region" aria-label={messages.assetsAudit.sourceRegion}>{page.source}</pre>
              : mode === 'reading' ? <div className="readingMarkdown" tabIndex={0} role="region" aria-label={title || labels.previewButton} dangerouslySetInnerHTML={{ __html: html }} />
                : <StaticPreviewFrame source={themedHtml} title={title} />}
          </>}
    </div>
  </>
}
