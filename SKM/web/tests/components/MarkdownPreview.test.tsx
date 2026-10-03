import { renderToStaticMarkup } from 'react-dom/server'
import { marked } from 'marked'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MarkdownDocumentPreview } from '../../src/components/MarkdownDocumentPreview'
import { MarkdownText } from '../../src/components/MarkdownText'
import { HtmlPreview } from '../../src/components/HtmlPreview'
import { SourcePreview } from '../../src/components/SourcePreview'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { useMarkdownPreview } from '../../src/hooks/useMarkdownPreview'

vi.mock('../../src/hooks/useMarkdownPreview', () => ({ useMarkdownPreview: vi.fn() }))
const preview = vi.mocked(useMarkdownPreview)
afterEach(() => vi.resetAllMocks())

/** 両入口は同じ hook と頁 UI を共有し、context の安全 renderer だけを選ぶ。 */
describe('shared Markdown preview surfaces', () => {
  it('shares pagination in document management and excerpt/report previews', () => {
    const source = '# Hello\n\n**bold**'
    preview.mockReturnValue({ status: 'ready', failure: null, index: 1, count: 3, select: vi.fn(),
      page: { source, tokens: marked.lexer(source), heading: 'Hello', sourceOnly: false } })
    const document = renderToStaticMarkup(<MarkdownDocumentPreview source={source} title="Document" />)
    const excerpt = renderToStaticMarkup(<MarkdownText text={source} />)
    expect(document).toContain('markdownPreviewPagination')
    expect(excerpt).toContain('markdownPreviewPagination')
    expect(document).toContain('sandbox=""')
    expect(excerpt).toContain('<h4>Hello</h4>')
    expect(excerpt).toContain('<strong>bold</strong>')
    expect(preview).toHaveBeenCalledWith(source, true, 'document')
    expect(preview).toHaveBeenCalledWith(source, true, 'reading')
  })

  it.each(['zh', 'ja', 'en'] as const)('explains a complex source-only page in %s and keeps it inert', (language) => {
    const source = '<script>alert(1)</script>'
    preview.mockReturnValue({ status: 'ready', failure: null, index: 0, count: 2, select: vi.fn(),
      page: { source, tokens: [], heading: '', sourceOnly: true } })
    const html = renderToStaticMarkup(<LanguageProvider language={language}><MarkdownText text={source} /></LanguageProvider>)
    expect(html).toContain(MESSAGES[language].documentsPanel.markdownSourcePage)
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>')
  })

  it('shows a loading state instead of stale content, and clearly identifies parse failures', () => {
    preview.mockReturnValue({ status: 'loading', failure: null, index: 0, count: 0, select: vi.fn(), page: null })
    expect(renderToStaticMarkup(<MarkdownText text="loading" />)).toContain('role="status"')
    preview.mockReturnValue({ status: 'error', failure: 'timeout', index: 0, count: 2, select: vi.fn(),
      page: { source: 'safe bounded source', tokens: [], heading: '', sourceOnly: true } })
    const html = renderToStaticMarkup(<MarkdownText text="large" />)
    expect(html).toContain(MESSAGES.zh.documentsPanel.markdownPreviewFailed)
    expect(html).toContain('safe bounded source')
    expect(html).toContain('markdownPreviewPagination')
  })
})

/** 原文と HTML は入場後も一度に巨大 DOM を構築しない。 */
describe('bounded raw source and HTML', () => {
  it('renders only the first source page and names the partial display', () => {
    const html = renderToStaticMarkup(<SourcePreview source={'a'.repeat(32_000) + 'hidden tail'} />)
    expect(html).toContain('markdownPreviewPagination')
    expect(html).toContain(MESSAGES.zh.documentsPanel.sourcePageHint)
    expect(html).not.toContain('hidden tail')
  })
  it('rejects text beyond the same 20 MB input policy', () => {
    const html = renderToStaticMarkup(<SourcePreview source={'a'.repeat(20_000_001)} />)
    expect(html).toContain('20 MB')
    expect(html).not.toContain('<pre')
  })
  it('keeps bounded HTML sandboxed and displays large HTML as explicitly paged source', () => {
    const small = renderToStaticMarkup(<HtmlPreview source="<h1>Hello</h1>" title="HTML" />)
    expect(small).toContain('sandbox=""')
    const large = renderToStaticMarkup(<HtmlPreview source={'<div>x</div>'.repeat(100_000)} title="HTML" />)
    expect(large).toContain(MESSAGES.zh.documentsPanel.htmlSourcePages)
    expect(large).toContain('markdownPreviewPagination')
    expect(large).not.toContain('<iframe')
    expect(large).toContain('&lt;div&gt;')
  })
})
