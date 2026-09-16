import { describe, expect, it } from 'vitest'
import { readingMarkdown } from '../../src/lib/markdown'

describe('static reading Markdown', () => {
  it('formats headings, tables, lists and code without changing source', () => {
    const source = '# Scope\n\n**Two** documents\n\n| File | Result |\n| --- | --- |\n| A | Reviewed |\n\n- first\n- second\n\n```sql\nselect "field";\n```'
    const html = readingMarkdown(source)
    expect(html).toContain('<h4>Scope</h4>')
    expect(html).toContain('<strong>Two</strong>')
    expect(html).toContain('<table>')
    expect(html).toContain('<li>first</li>')
    expect(html).toContain('select &quot;field&quot;;')
  })

  it.each([
    '<script>alert(1)</script><style>body{display:none}</style>',
    '<img src="https://example.invalid/a" onerror="alert(1)">',
    '<svg onload="alert(1)"><use href="https://example.invalid/a"/></svg>',
    '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    '<a href="javascript:alert(1)">open</a>',
  ])('keeps raw markup inert: %s', (source) => {
    const html = readingMarkdown(source)
    expect(html).not.toMatch(/<(script|style|img|svg|iframe|a)\b/i)
    expect(html).toContain('&lt;')
  })

  it('renders link labels and image alternatives without resource or navigation attributes', () => {
    const html = readingMarkdown('[**label**](javascript:alert(1)) ![diagram](https://example.invalid/a.png) <https://example.invalid>')
    expect(html).toContain('<strong>label</strong>')
    expect(html).toContain('diagram')
    expect(html).not.toMatch(/<(a|img)\b|\b(?:href|src)=/)
  })

  it('does not allow a code language to escape its class attribute', () => {
    const html = readingMarkdown('```x" onmouseover="alert(1)\n<svg onload=alert(1)>\n```')
    expect(html).not.toContain('<svg')
    expect(html).not.toContain('class="language-x"')
  })

  it('keeps oversized text available without Markdown parsing', () => {
    const source = '<script>' + 'x'.repeat(1_000_001)
    expect(readingMarkdown(source)).toBe('<pre>&lt;script&gt;' + 'x'.repeat(1_000_001) + '</pre>')
  })
})
