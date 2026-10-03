import { Marked, type Token } from 'marked'
import { mermaidCodeHtml, type MermaidPreviews, type MermaidPreviewLabels } from './mermaidPreview'

/** 原 HTML は文字として保持し、リンクと画像は外部接続しない本文へ変換する。 */
function createReader(mermaid?: { previews: MermaidPreviews; labels: MermaidPreviewLabels }) {
  return new Marked({
    gfm: true,
    breaks: true,
    renderer: {
      code(token) { return mermaid ? mermaidCodeHtml(token, mermaid.previews, 'reading', mermaid.labels) : false },
      html({ text }) { return escapeMarkup(text) },
      link({ tokens }) { return this.parser.parseInline(tokens) },
      image({ text }) { return escapeMarkup(text) },
      checkbox({ checked }) { return checked ? '☑ ' : '☐ ' },
      heading({ tokens, depth }) {
        const level = Math.min(depth + 3, 6)
        return `<h${level}>${this.parser.parseInline(tokens)}</h${level}>`
      },
    },
  })
}

/** 解析済みの現在頁に同じ静的 allowlist を適用し、raw HTML を実行しない。 */
export function readingMarkdownPage(tokens: Token[], source: string, mermaid?: { previews: MermaidPreviews; labels: MermaidPreviewLabels }): string {
  try { return createReader(mermaid).parser(tokens) }
  catch { return `<pre>${escapeMarkup(source)}</pre>` }
}

/** raw HTML・画像代替文を markup として解釈させない。 */
function escapeMarkup(text: string): string {
  return text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;')
}
