import { marked, type Token, type Tokens } from 'marked'

// 行数だけでは幅広い Excel 表を制限できないため、cell 数と原文量も予算に含める。
const PAGE_WEIGHT = 32_000
const TABLE_ROWS = 80
const CELL_WEIGHT = 40

/** 原文表示は全体を保持し、preview はこの token 群だけを DOM 化する。 */
export interface MarkdownPreviewPage {
  tokens: Token[]
  source: string
  heading: string
}

/** GFM の表は行境界で分けて表頭を再掲する。他の block 内は切らない。 */
function tableParts(table: Tokens.Table): Tokens.Table[] {
  const lines = table.raw.trimEnd().split('\n')
  // lexer の将来変更で行対応が不明になった場合、原文を勝手に組み替えない。
  if (lines.length !== table.rows.length + 2 || table.rows.length === 0) return [table]
  const header = lines.slice(0, 2).join('\n') + '\n'
  const headerWeight = header.length + table.header.length * CELL_WEIGHT
  const parts: Tokens.Table[] = []
  let start = 0
  while (start < table.rows.length) {
    let end = start
    let weight = headerWeight
    while (end < table.rows.length && end - start < TABLE_ROWS) {
      const nextWeight = lines[end + 2]!.length + table.rows[end]!.length * CELL_WEIGHT
      if (end > start && weight + nextWeight > PAGE_WEIGHT) break
      weight += nextWeight
      end++
    }
    parts.push({ ...table, rows: table.rows.slice(start, end), raw: header + lines.slice(start + 2, end + 2).join('\n') + '\n' })
    start = end
  }
  return parts
}

/** 全文を一度だけ lex して参照 link/コードを保持し、現在頁だけの描画へ渡す。 */
export function markdownPreviewPages(source: string): MarkdownPreviewPage[] {
  const pages: MarkdownPreviewPage[] = []
  let tokens: Token[] = []
  let weight = 0
  let heading = ''
  let pageHeading = ''
  const flush = (): void => {
    if (!tokens.length) return
    pages.push({ tokens, source: tokens.map((token) => token.raw).join(''), heading: pageHeading })
    tokens = []
    weight = 0
  }
  for (const token of marked.lexer(source, { gfm: true })) {
    const parts = token.type === 'table' ? tableParts(token as Tokens.Table) : [token]
    for (const [partIndex, part] of parts.entries()) {
      const cells = part.type === 'table'
        ? (part as Tokens.Table).header.length + (part as Tokens.Table).rows.reduce((count, row) => count + row.length, 0) : 0
      const nextWeight = part.raw.length + cells * CELL_WEIGHT
      if (partIndex > 0 || weight && weight + nextWeight > PAGE_WEIGHT) flush()
      if (part.type === 'heading') heading = (part as Tokens.Heading).text
      if (!tokens.length || !pageHeading) pageHeading = heading
      tokens.push(part)
      weight += nextWeight
    }
  }
  flush()
  // 非 DOM 環境の fallback でも、単頁なら CRLF/参照定義を含む原文をそのまま使う。
  if (pages.length <= 1) return [{ tokens: pages[0]?.tokens ?? [], source, heading: pages[0]?.heading ?? '' }]
  return pages
}
