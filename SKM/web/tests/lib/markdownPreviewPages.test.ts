import { marked, type Tokens } from 'marked'
import { describe, expect, it, vi } from 'vitest'
import { documentMarkdownHtml, documentMarkdownPageHtml } from '../../src/lib/documentPreview'
import { MARKDOWN_PREVIEW_MAX_PAGES, MARKDOWN_PREVIEW_PAGE_CHARACTERS, markdownPreviewPages, markdownSourcePage, markdownSourcePageCount } from '../../src/lib/markdownPreviewPages'

/** 幅広い表でも全行を順序通り保ち、Markdown 構文を途中で切らない。 */
describe('Markdown preview pages', () => {
  it('splits wide tables, repeats headers and keeps every data row exactly once', () => {
    const header = '| ' + Array.from({ length: 40 }, (_, index) => `C${index}`).join(' | ') + ' |\n'
    const source = '# Sheet\n\n' + header + '| ' + Array(40).fill('---').join(' | ') + ' |\n'
      + Array.from({ length: 300 }, (_, row) => '| ' + Array.from({ length: 40 }, (_, col) => `R${row}C${col}`).join(' | ') + ' |\n').join('')
      + '\n## Review\n\nLast paragraph.\n'
    const pages = markdownPreviewPages(source)
    expect(pages.length).toBeGreaterThan(10)
    const tables = pages.flatMap((page) => page.tokens.filter((token): token is Tokens.Table => token.type === 'table'))
    expect(tables.flatMap((table) => table.rows.map((row) => row[0]!.text)))
      .toEqual(Array.from({ length: 300 }, (_, row) => `R${row}C0`))
    expect(tables.every((table) => table.header.length === 40 && table.rows.length <= 80)).toBe(true)
    expect(tables.every((table) => table.raw.startsWith(header))).toBe(true)
    expect(pages.map((page) => marked.parser(page.tokens)).join('')).toContain('Last paragraph.')
    expect(pages[1]!.heading).toBe('Sheet')
  })

  it('keeps escaped pipes, line breaks, formatting, alignment and reference links across pages', () => {
    const source = '| A | B |\n| :--- | ---: |\n' + Array(165).fill('| a\\|b | **bold**<br>[label][ref] |\n').join('')
      + '\n[ref]: https://example.invalid\n'
    const pages = markdownPreviewPages(source)
    expect(pages.length).toBe(3)
    for (const page of pages) {
      const html = marked.parser(page.tokens)
      expect(html).toContain('a|b')
      expect(html).toContain('<strong>bold</strong><br>')
      expect(html).toContain('href="https://example.invalid"')
      expect(html).toContain('align="right"')
    }
  })

  it('retains empty documents and exact single-page source including CRLF and definitions', () => {
    for (const source of ['', '# 日本語\r\n\r\n[x][r]\r\n\r\n[r]: /ref\r\n']) {
      expect(markdownPreviewPages(source)).toHaveLength(1)
      expect(markdownPreviewPages(source)[0]!.source).toBe(source)
    }
  })

  it('does not split fenced code, raw HTML or nested lists as if they were top-level tables', () => {
    const source = '```md\n| A | B |\n| --- | --- |\n' + '| x | y |\n'.repeat(40) + '```\n\n'
      + '- outer\n  - inner\n\n<div>raw</div>\n'
    const tokens = markdownPreviewPages(source).flatMap((page) => page.tokens)
    expect(tokens.filter((token) => token.type === 'code')).toHaveLength(1)
    expect(tokens.some((token) => token.type === 'table')).toBe(false)
    expect(marked.parser(tokens)).toContain('<div>raw</div>')
    expect(marked.parser(tokens)).toContain('<li>inner</li>')
  })

  it.each([
    (text: string) => `\`\`\`md\n${text}\n\`\`\`\n`,
    (text: string) => text + '\n',
    (text: string) => `<div>${text}</div>\n`,
    (text: string) => `- ${text}\n`,
  ])('bounds oversized atomic blocks as explicit source-only slices', (block) => {
    const source = block('字'.repeat(100_000))
    const pages = markdownPreviewPages(source)
    expect(pages.length).toBeGreaterThan(3)
    expect(pages.every((page) => page.sourceOnly && page.tokens.length === 0)).toBe(true)
    expect(pages.every((page) => page.source.length <= MARKDOWN_PREVIEW_PAGE_CHARACTERS)).toBe(true)
    expect(pages.map((page) => page.source).join('')).toBe(source)
  })

  it.each(['header', 'row'] as const)('bounds an oversized table %s without losing its text', (part) => {
    const huge = 'cell'.repeat(20_000)
    const source = `| ${part === 'header' ? huge : 'A'} | B |\n| --- | --- |\n| ${part === 'row' ? huge : 'x'} | y |\n`
    const pages = markdownPreviewPages(source)
    expect(pages.some((page) => page.sourceOnly)).toBe(true)
    expect(pages.every((page) => page.source.length <= MARKDOWN_PREVIEW_PAGE_CHARACTERS)).toBe(true)
    expect(pages.map((page) => page.source).join('')).toBe(source)
  })

  it('keeps surrogate pairs and every source character across fallback page boundaries', () => {
    const source = 'a'.repeat(31_998) + '😀'.repeat(40_000) + 'tail'
    const pages = Array.from({ length: markdownSourcePageCount(source) }, (_, index) => markdownSourcePage(source, index))
    expect(pages.map((page) => page.source).join('')).toBe(source)
    expect(pages.every((page) => page.source.length <= MARKDOWN_PREVIEW_PAGE_CHARACTERS && !/^[\uDC00-\uDFFF]|[\uD800-\uDBFF]$/.test(page.source))).toBe(true)
  })

  it('limits a tiny reference token with a giant resolved destination before cloning or rendering', () => {
    const source = '[x][r]\n\n[r]: https://example.invalid/' + 'a'.repeat(100_000)
    const pages = markdownPreviewPages(source)
    expect(pages[0]!.sourceOnly).toBe(true)
    expect(pages[0]!.tokens).toHaveLength(0)
    expect(pages.map((page) => page.source).join('')).toBe(source)
    expect(pages.every((page) => page.source.length <= MARKDOWN_PREVIEW_PAGE_CHARACTERS)).toBe(true)
  })

  it('caps repeated resolved-reference page amplification and preserves the complete admitted source', () => {
    const source = '[x][r]\n\n'.repeat(50_000) + '[r]: https://example.invalid/' + 'a'.repeat(16_000)
    expect(source.length).toBeLessThan(1_000_000)
    const pages = markdownPreviewPages(source)
    expect(pages.length).toBeLessThanOrEqual(MARKDOWN_PREVIEW_MAX_PAGES)
    expect(pages.length).toBe(markdownSourcePageCount(source))
    expect(pages.every((page) => page.sourceOnly && page.tokens.length === 0)).toBe(true)
    expect(pages.every((page) => page.source.length <= MARKDOWN_PREVIEW_PAGE_CHARACTERS)).toBe(true)
    expect(pages.map((page) => page.source).join('')).toBe(source)
  })

  it('stops page generation at the metadata budget rather than allocating every amplified page', () => {
    const href = 'https://example.invalid/' + 'a'.repeat(16_000)
    const source = '[x][r]\n\n'.repeat(50_000) + '[r]: ' + href
    let rawReads = 0
    const token: Tokens.Paragraph = { type: 'paragraph', get raw() { rawReads++; return '[x][r]\n\n' }, text: '[x][r]',
      tokens: [{ type: 'link', raw: '[x][r]', href, text: 'x', tokens: [{ type: 'text', raw: 'x', text: 'x' }] }] }
    const tokens = Object.assign(Array.from({ length: 50_000 }, () => token), { links: {} })
    const lexer = vi.spyOn(marked, 'lexer').mockReturnValueOnce(tokens)
    try {
      const pages = markdownPreviewPages(source)
      expect(pages.every((page) => page.sourceOnly)).toBe(true)
      expect(rawReads).toBeLessThan(MARKDOWN_PREVIEW_MAX_PAGES * 4)
      expect(pages.map((page) => page.source).join('')).toBe(source)
    } finally { lexer.mockRestore() }
  })

  it('keeps a smaller amplified document bounded on the synchronous preview path', () => {
    const source = '[x][r]\n\n'.repeat(1000) + '[r]: https://example.invalid/' + 'a'.repeat(16_000)
    expect(source.length).toBeLessThanOrEqual(MARKDOWN_PREVIEW_PAGE_CHARACTERS)
    const pages = markdownPreviewPages(source)
    expect(pages.length).toBeLessThanOrEqual(MARKDOWN_PREVIEW_MAX_PAGES)
    expect(pages.map((page) => page.source).join('')).toBe(source)
  })

  it('preserves reading-mode newline breaks and document-mode paragraph semantics', () => {
    const source = 'first\nsecond\n'
    expect(marked.parser(markdownPreviewPages(source, 'reading')[0]!.tokens)).toContain('<br>')
    expect(marked.parser(markdownPreviewPages(source)[0]!.tokens)).not.toContain('<br>')
  })

  it('falls back to Markdown rather than intermediate HTML without a DOM', () => {
    const source = '# Title\n\n| A |\n| --- |\n| <script>unsafe</script> |\n'
    const page = markdownPreviewPages(source)[0]!
    for (const output of [documentMarkdownHtml(source), documentMarkdownPageHtml(page.tokens, page.source)]) {
      expect(output).toContain('<pre># Title\n')
      expect(output).toContain('&lt;script&gt;unsafe&lt;/script&gt;')
      expect(output).not.toContain('&lt;!doctype html&gt;')
      expect(output).not.toContain('<script>')
    }
  })
})
