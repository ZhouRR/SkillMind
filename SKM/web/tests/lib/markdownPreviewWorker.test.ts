import { marked } from 'marked'
import { describe, expect, it, vi } from 'vitest'
import { createMarkdownPreviewWorkerModel } from '../../src/lib/markdownPreviewWorker'
import { DOCUMENT_PREVIEW_MAX_BYTES } from '../../src/lib/previewLimits'

/** browser transport と分離し、全文解析の一回性と一頁だけの配送を検証する。 */
describe('Markdown preview worker model', () => {
  it('keeps Markdown semantics above the former million-character limit and sends only one page at a time', () => {
    const source = '# Large report\n\n' + 'A **strong** [reference][r].\n\n'.repeat(36_000) + '\n[r]: https://example.invalid\n'
    expect(source.length).toBeGreaterThan(1_000_000)
    const model = createMarkdownPreviewWorkerModel()
    const first = model({ type: 'load', source, requestId: 1, mode: 'reading' })
    expect(first.type).toBe('page')
    if (first.type !== 'page') throw new Error('Expected parsed page')
    expect(first.count).toBeGreaterThan(1)
    expect(first.page.sourceOnly).toBe(false)
    expect(marked.parser(first.page.tokens)).toContain('<strong>strong</strong>')
    expect(marked.parser(first.page.tokens)).toContain('href="https://example.invalid"')
    expect(Object.keys(first).sort()).toEqual(['count', 'index', 'page', 'requestId', 'type'])
    expect(JSON.stringify(first).length).toBeLessThan(50_000)
    const lexer = vi.spyOn(marked, 'lexer')
    try {
      const last = model({ type: 'page', index: first.count - 1, requestId: 2 })
      expect(last.type).toBe('page')
      expect(lexer).not.toHaveBeenCalled()
      if (last.type === 'page') expect(marked.parser(last.page.tokens)).toContain('<strong>strong</strong>')
    } finally { lexer.mockRestore() }
  })

  it('preflights dense twenty-megabyte Markdown before lexer allocation and preserves bounded source pages', () => {
    const source = 'a\n\n'.repeat(Math.floor(DOCUMENT_PREVIEW_MAX_BYTES / 3)) + 'xx'
    expect(source.length).toBe(DOCUMENT_PREVIEW_MAX_BYTES)
    const model = createMarkdownPreviewWorkerModel()
    const lexer = vi.spyOn(marked, 'lexer')
    try {
      const first = model({ type: 'load', source, requestId: 1, mode: 'document' })
      expect(first.type).toBe('page')
      expect(lexer).not.toHaveBeenCalled()
      if (first.type !== 'page') throw new Error('Expected bounded source page')
      expect(first.page.sourceOnly).toBe(true)
      expect(first.page.source.length).toBeLessThanOrEqual(32_000)
      let restored = ''
      for (let index = 0; index < first.count; index++) {
        const result = model({ type: 'page', index, requestId: index + 2 })
        if (result.type !== 'page') throw new Error('Expected source page')
        expect(result.page.tokens).toHaveLength(0)
        expect(result.page.source.length).toBeLessThanOrEqual(32_000)
        restored += result.page.source
      }
      expect(restored).toBe(source)
    } finally { lexer.mockRestore() }
  })

  it('rejects exact byte overflow before lexing and clears pages from an earlier document', () => {
    const model = createMarkdownPreviewWorkerModel()
    model({ type: 'load', source: '# Old', requestId: 1, mode: 'document' })
    const lexer = vi.spyOn(marked, 'lexer')
    try {
      expect(model({ type: 'load', source: 'x'.repeat(DOCUMENT_PREVIEW_MAX_BYTES + 1), requestId: 2, mode: 'document' }))
        .toEqual({ type: 'error', requestId: 2, failure: 'tooLarge' })
      expect(lexer).not.toHaveBeenCalled()
      expect(model({ type: 'page', index: 0, requestId: 3 })).toEqual({ type: 'error', requestId: 3, failure: 'parseFailed' })
    } finally { lexer.mockRestore() }
  })

  it('reports lexer errors and invalid page requests without leaking an earlier page', () => {
    const model = createMarkdownPreviewWorkerModel()
    const lexer = vi.spyOn(marked, 'lexer').mockImplementationOnce(() => { throw new Error('bad document') })
    try {
      expect(model({ type: 'load', source: '# Broken', requestId: 1, mode: 'document' }))
        .toEqual({ type: 'error', requestId: 1, failure: 'parseFailed' })
    } finally { lexer.mockRestore() }
    for (const index of [-1, 0, 2, Number.NaN, 0.5]) {
      expect(model({ type: 'page', index, requestId: 2 })).toEqual({ type: 'error', requestId: 2, failure: 'parseFailed' })
    }
  })
})
