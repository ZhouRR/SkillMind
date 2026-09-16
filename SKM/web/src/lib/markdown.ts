import { Marked } from 'marked'

/** 原 HTML は文字として保持し、リンクと画像は外部接続しない本文へ変換する。 */
const reader = new Marked({
  gfm: true,
  breaks: true,
  renderer: {
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

/** 報告と抜粋の静的 Markdown。巨大/不正な入力でも原文を失わず表示する。 */
export function readingMarkdown(source: string): string {
  if (source.length <= 1_000_000) {
    try { return reader.parse(source, { async: false }) } catch { /* 表示失敗は業務結果を変えない。 */ }
  }
  return `<pre>${escapeMarkup(source)}</pre>`
}

/** raw HTML・画像代替文を markup として解釈させない。 */
function escapeMarkup(text: string): string {
  return text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;')
}
