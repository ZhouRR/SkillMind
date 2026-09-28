import { marked, type Tokens } from 'marked'
import { describe, expect, it } from 'vitest'
import { documentMarkdownHtml, documentMarkdownPageHtml } from '../../src/lib/documentPreview'
import { markdownPreviewPages } from '../../src/lib/markdownPreviewPages'

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
    const source = '```md\n| A | B |\n| --- | --- |\n' + '| x | y |\n'.repeat(4000) + '```\n\n'
      + '- outer\n  - inner\n\n<div>raw</div>\n'
    const tokens = markdownPreviewPages(source).flatMap((page) => page.tokens)
    expect(tokens.filter((token) => token.type === 'code')).toHaveLength(1)
    expect(tokens.some((token) => token.type === 'table')).toBe(false)
    expect(marked.parser(tokens)).toContain('<div>raw</div>')
    expect(marked.parser(tokens)).toContain('<li>inner</li>')
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
